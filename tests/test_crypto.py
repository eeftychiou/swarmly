"""
Unit tests for A2A Zero-Trust Cryptographic Identity and Authentication.
"""

import time
import unittest
from swarmly.crypto import CryptoAuth, KeyPair


class TestCryptoAuth(unittest.TestCase):

    def setUp(self):
        self.crypto = CryptoAuth(nonce_ttl_seconds=5)
        self.kp = KeyPair.generate_hmac("test_agent")

    def test_keypair_generation(self):
        self.assertEqual(self.kp.agent_id, "test_agent")
        self.assertEqual(self.kp.key_type, "hmac")
        self.assertTrue(len(self.kp.private_key) >= 32)
        self.assertEqual(self.kp.private_key, self.kp.public_key)

    def test_sign_and_verify_valid(self):
        payload = {"task": "TASK-001", "score": 236.48, "status": "APPROVED"}
        sig = self.crypto.sign_payload(payload, self.kp.private_key, self.kp.key_type)
        
        self.assertTrue(sig.startswith("hmac-sha256:"))
        is_valid = self.crypto.verify_payload(payload, sig, self.kp.public_key, "hmac")
        self.assertTrue(is_valid)

    def test_tampered_payload_rejected(self):
        payload = {"task": "TASK-001", "score": 236.48}
        sig = self.crypto.sign_payload(payload, self.kp.private_key, self.kp.key_type)
        
        # Tamper with payload
        tampered = {"task": "TASK-001", "score": 420.00}
        self.assertFalse(self.crypto.verify_payload(tampered, sig, self.kp.public_key, "hmac"))

    def test_wrong_key_rejected(self):
        payload = {"claim": "runway_congestion"}
        sig = self.crypto.sign_payload(payload, self.kp.private_key, self.kp.key_type)
        
        wrong_kp = KeyPair.generate_hmac("rogue_agent")
        self.assertFalse(self.crypto.verify_payload(payload, sig, wrong_kp.public_key, "hmac"))

    def test_challenge_response_handshake(self):
        token, nonce_str = self.crypto.create_challenge_nonce()
        self.assertIn(":", nonce_str)

        # Agent signs the nonce challenge
        sig = self.crypto.sign_payload({"nonce": nonce_str}, self.kp.private_key, self.kp.key_type)
        
        # Server verifies
        is_verified = self.crypto.verify_challenge_response(nonce_str, sig,
                                                              self.kp.public_key, "hmac")
        self.assertTrue(is_verified)

        # Replay attack: using the exact same nonce twice should fail
        is_replayed = self.crypto.verify_challenge_response(nonce_str, sig,
                                                              self.kp.public_key, "hmac")
        self.assertFalse(is_replayed)

    def test_expired_nonce_rejected(self):
        short_crypto = CryptoAuth(nonce_ttl_seconds=0)  # expires immediately
        token, nonce_str = short_crypto.create_challenge_nonce()
        time.sleep(0.01)

        sig = short_crypto.sign_payload({"nonce": nonce_str}, self.kp.private_key, self.kp.key_type)
        self.assertFalse(short_crypto.verify_challenge_response(nonce_str, sig,
                                                                self.kp.public_key, "hmac"))

    def test_revocation_list(self):
        revocation = ["rogue_agent_1", "compromised_bot"]
        self.assertTrue(self.crypto.is_revoked("rogue_agent_1", revocation))
        self.assertFalse(self.crypto.is_revoked("test_agent", revocation))


if __name__ == "__main__":
    unittest.main()
