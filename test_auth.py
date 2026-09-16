"""Run with python -m unittest -v. No database or live provider is required."""

import importlib
import json
import os
import time
import unittest
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient


ENV = {
    "MYSQL_HOST": "unused", "MYSQL_USER": "unused", "MYSQL_PASSWORD": "unused",
    "MYSQL_DATABASE": "test", "AUTH_TOKEN": "test-static-secret",
    "OAUTH_ISSUER": "https://identity.example/",
    "OAUTH_JWKS_URL": "https://identity.example/.well-known/jwks.json",
    "OAUTH_RESOURCE_URL": "https://mcp.example/mcp",
    "OAUTH_REQUIRED_SCOPES": "mysql:read",
}
with patch.dict(os.environ, ENV, clear=True):
    import server


class AuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.private_key.public_key()))
        cls.jwk.update(kid="test-key", use="sig", alg="RS256")

    def setUp(self):
        # The MCP session manager has a single-use lifespan; each test needs a
        # fresh server, as it would have in a separate application process.
        with patch.dict(os.environ, ENV, clear=True):
            importlib.reload(server)
        self.fetch = patch.object(server.jwks_client, "fetch_data", return_value={"keys": [self.jwk]})
        self.fetch_mock = self.fetch.start()
        self.addCleanup(self.fetch.stop)
        self.client = TestClient(server.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def token(self, *, remove=(), key=None, **overrides):
        claims = {
            "iss": ENV["OAUTH_ISSUER"], "aud": ENV["OAUTH_RESOURCE_URL"],
            "sub": "test-user", "exp": int(time.time()) + 300,
            "scope": "mysql:read",
        }
        claims.update(overrides)
        for name in remove:
            claims.pop(name)
        return jwt.encode(claims, key or self.private_key, algorithm="RS256", headers={"kid": "test-key"})

    def rpc(self, token=None, method="tools/list", params=None):
        headers = {"Accept": "application/json, text/event-stream"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        return self.client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
        })

    def payload(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        if response.headers["content-type"].startswith("text/event-stream"):
            return json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith("data: ")))
        return response.json()

    def test_static_and_oauth_initialize_and_list_tools(self):
        for token in (ENV["AUTH_TOKEN"], self.token()):
            with self.subTest(method="initialize", token_type=token[:8]):
                result = self.payload(self.rpc(token, "initialize", {
                    "protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "auth-test", "version": "1"},
                }))
                self.assertEqual(result["result"]["serverInfo"]["name"], "mysql-readonly")
            result = self.payload(self.rpc(token))
            self.assertEqual({tool["name"] for tool in result["result"]["tools"]},
                             {"list_tables", "describe_table", "run_query"})

    def test_both_credentials_execute_read_only_tool(self):
        for token in (ENV["AUTH_TOKEN"], self.token()):
            with patch.object(server, "get_connection") as connect:
                cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
                cursor.fetchall.return_value = [{"value": 1}]
                result = self.payload(self.rpc(token, "tools/call", {
                    "name": "run_query", "arguments": {"sql": "SELECT 1 AS value"},
                }))
                self.assertFalse(result["result"].get("isError", False))
                cursor.execute.assert_called_once_with("SELECT 1 AS value LIMIT 200")

    def test_both_credentials_still_reject_writes(self):
        for token in (ENV["AUTH_TOKEN"], self.token()):
            with patch.object(server, "get_connection") as connect:
                result = self.payload(self.rpc(token, "tools/call", {
                    "name": "run_query", "arguments": {"sql": "DELETE FROM customers"},
                }))
                self.assertTrue(result["result"]["isError"])
                connect.assert_not_called()

    def test_discovery_and_challenge(self):
        for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["resource"], ENV["OAUTH_RESOURCE_URL"])
            self.assertEqual(response.json()["authorization_servers"], [ENV["OAUTH_ISSUER"]])
            self.assertEqual(self.client.head(path).status_code, 200)
            self.assertEqual(self.client.post(path).status_code, 401)
        response = self.rpc()
        self.assertEqual(response.status_code, 401)
        self.assertIn('resource_metadata="https://mcp.example/.well-known/oauth-protected-resource"',
                      response.headers["www-authenticate"])
        self.assertEqual(self.client.get("/.well-known/oauth-protected-resource/anything").status_code, 401)

    def test_invalid_tokens(self):
        tokens = ["wrong-static-token", "not.a.jwt", self.token(exp=int(time.time()) - 10),
                  self.token(iss="https://attacker.example"), self.token(aud="another-api"),
                  self.token(key=self.other_key), self.token(nbf=int(time.time()) + 300),
                  jwt.encode({"sub": "user"}, "secret", algorithm="HS256"),
                  jwt.encode({"sub": "user"}, "", algorithm="none")]
        tokens += [self.token(remove=(name,)) for name in ("exp", "iss", "aud", "sub")]
        for token in tokens:
            with self.subTest(token=token[:12]), patch.object(server, "get_connection") as connect:
                self.assertEqual(self.rpc(token).status_code, 401)
                connect.assert_not_called()

    def test_scope_enforcement(self):
        for scope in ("", "mysql:write", "mysql:readonly", ["mysql:read"], None):
            response = self.rpc(self.token(scope=scope))
            self.assertEqual(response.status_code, 403)
            self.assertIn('error="insufficient_scope"', response.headers["www-authenticate"])
        self.assertEqual(self.rpc(self.token(scope="openid mysql:read")).status_code, 200)

    def test_malformed_headers(self):
        for header in ("Basic test-static-secret", "Bearer", "Bearer ", "Bearer  test-static-secret", "Bearer abc def"):
            self.assertEqual(self.client.get("/mcp", headers={"Authorization": header}).status_code, 401)
        self.assertEqual(self.client.get("/mcp", headers=[("Authorization", "Bearer test-static-secret"),
                                                         ("Authorization", "Bearer other")]).status_code, 401)

    def test_provider_outage_preserves_static_auth(self):
        self.fetch_mock.side_effect = jwt.PyJWKClientConnectionError("offline")
        self.assertEqual(self.rpc(self.token()).status_code, 503)
        self.fetch_mock.reset_mock()
        self.assertEqual(self.rpc(ENV["AUTH_TOKEN"]).status_code, 200)
        self.fetch_mock.assert_not_called()

    def test_unknown_key_rejected(self):
        self.fetch_mock.return_value = {"keys": [{**self.jwk, "kid": "other-key"}]}
        self.assertEqual(self.rpc(self.token()).status_code, 401)

    def test_static_only_mode(self):
        with patch.object(server, "OAUTH", None):
            self.assertEqual(self.rpc(ENV["AUTH_TOKEN"]).status_code, 200)
            self.assertEqual(self.rpc(self.token()).status_code, 401)
            self.assertEqual(self.client.get("/.well-known/oauth-protected-resource").status_code, 401)


class ConfigurationTests(unittest.TestCase):
    def test_optional_config(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(server.oauth_configuration())

    def test_partial_config_is_rejected(self):
        with patch.dict(os.environ, {"OAUTH_ISSUER": "https://identity.example"}, clear=True):
            with self.assertRaises(ValueError):
                server.oauth_configuration()

    def test_invalid_config_is_rejected(self):
        for name, value in (("OAUTH_ISSUER", "http://identity.example"),
                            ("OAUTH_JWKS_URL", "https://user:pass@identity.example/keys"),
                            ("OAUTH_RESOURCE_URL", "https://mcp.example/mcp?query=1"),
                            ("OAUTH_REQUIRED_SCOPES", ""),
                            ("OAUTH_REQUIRED_SCOPES", 'bad"scope')):
            with self.subTest(name=name), patch.dict(os.environ, {**ENV, name: value}, clear=True):
                with self.assertRaises(ValueError):
                    server.oauth_configuration()

    def test_legacy_startup_without_oauth(self):
        import subprocess
        import sys
        env = {k: v for k, v in ENV.items() if not k.startswith("OAUTH_")}
        result = subprocess.run([sys.executable, "-c", "import server; assert server.OAUTH is None"],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
