"""Offline tests: committed native evidence passes, and representative mutations fail."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("qc_check_native", HERE / "check_native.py")
check_native = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_native)
OBSERVED = json.loads((HERE / "observed-native.json").read_text())
DIGEST = hashlib.sha256((HERE / "native_observe.py").read_bytes()).hexdigest()


def case(data, name):
    return next(row for row in data["cases"] if row["case"] == name)


class NativeEvidenceTests(unittest.TestCase):
    def test_committed_evidence_passes_and_is_sanitized(self):
        self.assertEqual(check_native.check(OBSERVED, DIGEST), [])
        text = json.dumps(OBSERVED)
        for forbidden in ("/tmp/", "/home/", "FABRICATED", "Bearer ", "sk-ant-"):
            self.assertNotIn(forbidden, text)

    def mutated(self, change):
        data = copy.deepcopy(OBSERVED)
        change(data)
        return check_native.check(data, DIGEST)

    def test_mutations_fail(self):
        mutations = {
            "model attempt": lambda d: case(d, "startup-valid").update(model_attempts=1),
            "refresh on inference host": lambda d: case(d, "expired-refresh-success")["requests"][0]
            .update(host="api.anthropic.com"),
            "old bearer after refresh": lambda d: case(d, "expired-refresh-success")["requests"][-1]
            .update(auth="bearer-A"),
            "no atomic rewrite": lambda d: case(d, "expired-refresh-success")["credential_after"]
            .update(inode_replaced=False),
            "loose credential mode": lambda d: case(d, "startup-valid")["credential_after"].update(mode="0o644"),
            "gate saw auxiliary traffic": lambda d: case(d, "startup-valid-base-url-gate")["requests"]
            .append({"host": "ANTHROPIC_BASE_URL", "method": "GET", "path": "/api/oauth/usage",
                     "auth": "bearer-A", "phase": "startup"}),
            "usage with nonessential off": lambda d: case(d, "startup-valid-nonessential-off")["requests"]
            .append({"host": "api.anthropic.com", "method": "GET", "path": "/api/oauth/usage",
                     "auth": "bearer-A", "phase": "after_get_usage"}),
            "unexpected host": lambda d: case(d, "startup-valid").update(refused_connect_hosts=["example.com"]),
            "wrong binary": lambda d: d.update(binary_sha256="0" * 64),
            "missing case": lambda d: d["cases"].pop(),
        }
        for name, change in mutations.items():
            with self.subTest(mutation=name):
                self.assertNotEqual(self.mutated(change), [])
        self.assertNotEqual(check_native.check(OBSERVED, "0" * 64), [])


if __name__ == "__main__":
    unittest.main()
