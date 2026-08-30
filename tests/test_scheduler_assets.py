from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class SchedulerAssetTests(unittest.TestCase):
    def test_locked_runner_uses_only_the_incremental_live_scan(self):
        runner = (PROJECT_ROOT / "scripts" / "run-production-scan.sh").read_text(encoding="utf-8")
        self.assertIn("flock -n 9", runner)
        self.assertIn('main.py" --live', runner)
        self.assertNotIn("--no-email", runner)
        self.assertNotIn("backfill", runner.lower())
        self.assertIn("SCAN_START", runner)
        self.assertIn("SCAN_END", runner)
        self.assertIn("next_scheduled_run", runner)

    def test_systemd_timer_is_persistent_and_every_ten_minutes(self):
        timer = (PROJECT_ROOT / "deploy" / "systemd" / "used-pc-finder.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* *:0/10:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_service_template_uses_runtime_lock_directory_and_network_ordering(self):
        service = (PROJECT_ROOT / "deploy" / "systemd" / "used-pc-finder.service.template").read_text(encoding="utf-8")
        self.assertIn("After=network-online.target", service)
        self.assertIn("StartLimitIntervalSec=0", service)
        self.assertIn("RuntimeDirectory=used-pc-finder", service)
        self.assertIn("USED_PC_FINDER_LOCK_FILE=/run/used-pc-finder/scan.lock", service)
        self.assertIn("run-production-scan.sh", service)


if __name__ == "__main__":
    unittest.main()
