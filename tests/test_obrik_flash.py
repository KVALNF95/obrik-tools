import tempfile
import unittest
from pathlib import Path
import subprocess
import sys

import obrik_flash


class ParamsTests(unittest.TestCase):
    def test_qgc_five_column_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.params"
            path.write_text("1\t1\tSYS_AUTOSTART\t4050\t6\n1\t1\tWV_GAIN\t1.5\t9\n")
            self.assertEqual(
                obrik_flash.parse_params_file(path),
                [("SYS_AUTOSTART", 4050.0, 6), ("WV_GAIN", 1.5, 9)],
            )

    def test_int32_wire_roundtrip(self):
        for value in (-1, 0, 1, 4050):
            encoded = obrik_flash.encode_param_value(value, 6)
            self.assertEqual(obrik_flash.decode_param_value(encoded, 6), value)

    def test_process_output_reader(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(.1); print('done', flush=True)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        reader = obrik_flash.ProcessOutputReader(proc)
        output = reader.collect(3, idle_timeout=.5)
        proc.wait(timeout=3)
        reader.thread.join(timeout=1)
        proc.stdout.close()
        self.assertIn("ready", output)
        self.assertIn("done", output)


if __name__ == "__main__":
    unittest.main()
