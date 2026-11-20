"""When notifications can't work on this device, the Settings row names the real reason and the one fix for it,
instead of always pointing to the Home Screen."""

import json
import shutil
import subprocess
import unittest

from tests.webjs import WEB

SCRIPT = """
import { push } from "%(device)s";
const setup = ({ secure, sw, pm, notification, ua, standalone }) => {
  globalThis.isSecureContext = secure;
  const nav = { userAgent: ua, platform: "Linux", maxTouchPoints: 0, standalone };
  if (sw) nav.serviceWorker = {};
  Object.defineProperty(globalThis, "navigator", { value: nav, configurable: true });
  globalThis.window = globalThis;
  if (pm) globalThis.PushManager = class {}; else delete globalThis.PushManager;
  if (notification) globalThis.Notification = {}; else delete globalThis.Notification;
  globalThis.matchMedia = () => ({ matches: !!standalone });
  return push.problem();
};
const desktop = "Mozilla/5.0 (X11; Linux x86_64)";
const iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)";
console.log(JSON.stringify({
  ok: setup({ secure: true, sw: true, pm: true, notification: true, ua: desktop }),
  insecure: setup({ secure: false, sw: false, pm: true, notification: true, ua: desktop }),
  insecureIphone: setup({ secure: false, sw: false, pm: false, notification: false, ua: iphone }),
  iphoneBrowser: setup({ secure: true, sw: true, pm: false, notification: false, ua: iphone }),
  iphoneInstalledOld: setup({ secure: true, sw: true, pm: false, notification: false, ua: iphone, standalone: true }),
  noPush: setup({ secure: true, sw: true, pm: false, notification: true, ua: desktop }),
}));
"""


class NotificationsUnavailableReason(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "needs Node to run the client's code")
    def test_each_case_gets_its_own_reason(self):
        run = subprocess.run(
            ["node", "--input-type=module"],
            input=SCRIPT % {"device": (WEB / "device.js").as_uri()},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        self.assertIsNone(out["ok"])
        self.assertEqual(out["insecure"], "insecure")
        self.assertEqual(out["insecureIphone"], "insecure")
        self.assertEqual(out["iphoneBrowser"], "install")
        self.assertEqual(out["iphoneInstalledOld"], "unsupported")
        self.assertEqual(out["noPush"], "unsupported")


if __name__ == "__main__":
    unittest.main()
