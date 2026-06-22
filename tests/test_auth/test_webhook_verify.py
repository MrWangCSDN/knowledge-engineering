import hashlib, hmac
from src.service.scm.webhook_verify import verify_signature, parse_push


def test_verify_signature_ok_and_bad():
    secret, body = "whsec", b'{"a":1}'
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(secret, body, sig) is True
    assert verify_signature(secret, body, "sha256=deadbeef") is False
    assert verify_signature(secret, body, None) is False


def test_parse_push():
    payload = {"ref": "refs/heads/master", "after": "a"*40, "repository": {"id": 42}}
    ev = parse_push(payload)
    assert ev.ref == "master" and ev.after_sha == "a"*40 and ev.repo_external_id == 42
