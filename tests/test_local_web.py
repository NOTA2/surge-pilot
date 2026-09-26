import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from surgepilot.local_web import Controller, _parameters, make_handler


class LocalWebTests(unittest.TestCase):
    def test_parameters_reject_nonfinite_and_noninteger_lookback(self):
        for bad in ({"lookback": "2.5"}, {"rise": "NaN"}, {"stop": "-1"}, {"trail": "101"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _parameters(bad)
        self.assertEqual(_parameters({"lookback": 3, "rise": "5"})["rise"], "5")

    def test_loopback_api_requires_token_and_runs_fixed_report_action(self):
        controller = Controller()
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        except PermissionError:
            self.skipTest("local socket binding is blocked in this sandbox")
        server.RequestHandlerClass = make_handler(controller, server.server_port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            def submit(token):
                request = urllib.request.Request(origin + "/api/run", method="POST",
                    data=json.dumps({"action": "report", "lookback": 3, "rise": 5}).encode(),
                    headers={"Origin": origin, "Content-Type": "application/json",
                             "X-SurgePilot-Token": token})
                return urllib.request.urlopen(request, timeout=3)

            with self.assertRaises(urllib.error.HTTPError) as denied:
                submit("wrong")
            self.assertEqual(denied.exception.code, 403)
            denied.exception.close()
            with patch.object(controller, "_command", return_value=0) as command:
                with submit(controller.token) as response:
                    self.assertEqual(response.status, 202)
                for _ in range(40):
                    if controller.snapshot()["phase"] == "completed":
                        break
                    time.sleep(0.01)
                self.assertEqual(controller.snapshot()["phase"], "completed")
                self.assertEqual(command.call_args.args[0], "surgepilot.local_report")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
