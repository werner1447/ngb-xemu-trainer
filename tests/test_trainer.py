"""End-to-end tests against a fake xemu process (tests/fake_xbox.py).

Run:  .venv\\Scripts\\python -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import fake_xbox as fx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ngbtrainer import ngb, xbox  # noqa: E402
from ngbtrainer.cheats import (BytePattern, CheatTable, Pointer, add_to_table, read_value,  # noqa: E402
                               replace_in_table, table_entry)
from ngbtrainer.editor import Form, entry_from_form, form_from_entry  # noqa: E402
from ngbtrainer.hotkeys import PAD, ComboRecorder, Hotkeys, parse_keys, parse_pad  # noqa: E402
from ngbtrainer.scanner import Scanner, ScanError, ScannerShell, ScanSession  # noqa: E402
from ngbtrainer.trainer import Trainer  # noqa: E402
from ngbtrainer.winproc import find_processes  # noqa: E402
from ngbtrainer.worker import TrainerThread  # noqa: E402


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met")
        time.sleep(0.01)

HEALTH_VA = fx.PLAYER_VA + fx.HEALTH_OFFSET
HEALTH_PA = 0x400000 + fx.HEALTH_OFFSET
REPO = Path(__file__).resolve().parent.parent


class FakePad:
    """Stands in for XInput: FakePad.mask is what is pressed."""
    mask = 0
    connected = True

    def buttons(self):
        return FakePad.mask


class FakeXemuTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.Popen([sys.executable, str(Path(fx.__file__))],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        pid, base = cls.proc.stdout.readline().split()
        cls.pid, cls.base = int(pid), int(base)
        cls.mem = xbox.attach(cls.pid)

    @classmethod
    def tearDownClass(cls):
        cls.mem.close()
        cls.proc.stdin.close()
        cls.proc.wait(10)

    def setUp(self):
        self._set_health(fx.HEALTH)
        for pa in (fx.SYSDATA_PA, fx.STALE_PA):
            self.mem.write_phys(pa, fx.SYSDATA)
        self.mem.write_phys(fx.SYSDATA_PA + 0x1000, bytes(len(fx.SYSDATA)))
        self.mem.write_phys(fx.state_pa(ngb.INVENTORY), fx.inventory_bytes().ljust(ngb.SLOTS * 4, b"\0"))
        self.mem.write_phys(fx.state_pa(ngb.DIRTY_FLAGS), bytes(len(ngb.CHECKSUM_GROUPS)))
        self.mem.write_hook = None

    def _dirty_groups(self):
        flags = self.mem.read_phys(fx.state_pa(ngb.DIRTY_FLAGS), len(ngb.CHECKSUM_GROUPS))
        return [group for group, flag in enumerate(flags) if flag]

    def _slots(self):
        return [(slot.item, slot.count) for slot in ngb.inventory(self.mem)]

    def _set_health(self, value):
        self.mem.write_phys(HEALTH_PA, struct.pack("<H", value))

    def _health(self):
        return struct.unpack("<H", self.mem.read_phys(HEALTH_PA, 2))[0]

    def _table(self, data):
        path = Path(tempfile.mkdtemp()) / "cheats.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_finds_process_by_name(self):
        self.assertIn(self.pid, find_processes(Path(sys.executable).name))

    def test_locates_ram_and_skips_decoy(self):
        self.assertEqual(self.mem.host_base, self.base)
        self.assertEqual(self.mem.ram_size, fx.RAM_SIZE)

    def test_translate(self):
        self.assertEqual(self.mem.translate(0x10000), fx.XBE_PA)
        self.assertEqual(self.mem.translate(HEALTH_VA), HEALTH_PA)
        with self.assertRaises(xbox.PageFault):
            self.mem.translate(0x00900000)
        with self.assertRaises(xbox.PageFault):
            self.mem.translate(0x00030000)

    def test_xbe_info(self):
        game = xbox.read_xbe_info(self.mem)
        self.assertEqual(game.title_id, fx.TITLE_ID)
        self.assertEqual(game.title_name, fx.TITLE_NAME)
        self.assertEqual(game.title_code, "TC-013")
        self.assertEqual(game.region_names, "NTSC-U")
        self.assertTrue(game.contains(fx.PLAYER_POINTER_VA))
        self.assertFalse(game.contains(HEALTH_VA))

    def test_access_across_non_contiguous_pages(self):
        va = fx.SPLIT_VA - 2
        self.mem.write(va, b"ABCD")
        self.assertEqual(self.mem.read(va, 4), b"ABCD")
        self.assertEqual(self.mem.read_phys(fx.XBE_PA + 0xEFFE, 2), b"AB")
        self.assertEqual(self.mem.read_phys(fx.SPLIT_PA, 2), b"CD")

    def test_reverse_map_prefers_user_space(self):
        reverse = self.mem.reverse_map()
        self.assertEqual(reverse.lookup(fx.XBE_PA + 0x800)[0], fx.PLAYER_POINTER_VA)
        self.assertEqual(reverse.lookup(HEALTH_PA), [HEALTH_VA, fx.PLAYER_POOL_VA + fx.HEALTH_OFFSET])

    def test_pointer_chain(self):
        pointer = Pointer(fx.PLAYER_POINTER_VA, (fx.HEALTH_OFFSET,))
        self.assertEqual(pointer.resolve(self.mem), HEALTH_VA)
        self.assertEqual(read_value(self.mem, pointer.resolve(self.mem), "u16"), fx.HEALTH)
        self.assertEqual(str(pointer), "[0x00010800]+0x40")

    def test_scanner_exact_then_narrow(self):
        scanner = Scanner(self.mem)
        scanner.first("u16", str(fx.HEALTH))
        self.assertIn(HEALTH_PA // 2, scanner.addrs)
        self._set_health(150)
        scanner.next("<")
        scanner.next("=", "150")
        self.assertEqual([scanner.phys_address(1)], [HEALTH_PA])

    def test_scanner_unknown_value(self):
        scanner = Scanner(self.mem)
        scanner.first("u16", None)
        self._set_health(fx.HEALTH - 25)
        scanner.next("-=", "25")
        self.assertEqual(scanner.count, 1)
        self.assertEqual(scanner.phys_address(1), HEALTH_PA)

    def test_scanner_float_rounding(self):
        scanner = Scanner(self.mem)
        self.mem.write_phys(0x500000, struct.pack("<f", 99.7))
        scanner.first("f32", "100")
        self.assertIn(0x500000 // 4, scanner.addrs)
        scanner.first("f32", "99.5")
        self.assertNotIn(0x500000 // 4, scanner.addrs)

    def test_shell_ptr_and_add(self):
        table = self._table({"title_id": None, "cheats": []})
        shell = ScannerShell(self.mem, table)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            shell.onecmd(f"ptr 0x{HEALTH_VA:08X} 0x100")
            shell.onecmd('add 0x00010800 "Health" type=u16 offsets=0x40 key=F1 pad=back+dpad_up value=999')
            shell.onecmd("add 1 x")  # no hits yet -> reported, not raised
        text = out.getvalue()
        self.assertIn('"address": "0x00010800", "offsets": ["0x40"]', text)
        self.assertIn('"address": "0x00010810", "offsets": ["0x40"]', text)  # via the 0xD... alias
        self.assertIn("2 pointer(s) found", text)
        self.assertIn("[static]", text)
        self.assertIn("Error:", text)
        data = json.loads(table.read_text(encoding="utf-8"))
        self.assertEqual(data["title_id"], "0x5443000D")
        self.assertEqual(data["cheats"], [{"name": "Health", "type": "u16", "address": "0x00010800",
                                           "offsets": ["0x40"], "value": 999, "hotkey": "F1",
                                           "pad": "BACK+DPAD_UP"}])

    def test_trainer_freeze_value(self):
        trainer = Trainer(self._table({"title_id": "0x5443000D", "cheats": [
            {"name": "Health", "type": "u16", "address": "0x00010800", "offsets": ["0x40"], "value": 999}]}),
            pid=self.pid)
        cheat = trainer.table.cheats[0]
        trainer.tick()
        self.assertTrue(trainer.ready, trainer.status)
        self.assertEqual(self._health(), fx.HEALTH)  # off by default
        trainer.set_enabled(cheat, True)
        self._set_health(5)
        trainer.tick()
        self.assertEqual(self._health(), 999)
        self.assertIsNone(cheat.error)
        trainer.set_enabled(cheat, False)
        self._set_health(5)
        trainer.tick()
        self.assertEqual(self._health(), 5)
        trainer.disconnect()

    def test_trainer_capture_and_copy_from(self):
        trainer = Trainer(self._table({"cheats": [
            {"name": "Freeze", "type": "u16", "address": f"0x{HEALTH_VA:08X}"},
            {"name": "Refill", "type": "u16", "address": f"0x{HEALTH_VA:08X}", "mode": "once",
             "copy_from": {"address": "0x00010800", "offsets": [fx.MAX_HEALTH_OFFSET]}}]}), pid=self.pid)
        freeze, refill = trainer.table.cheats
        trainer.tick()
        trainer.set_enabled(freeze, True)
        self._set_health(10)
        trainer.tick()
        self.assertEqual(self._health(), fx.HEALTH)
        trainer.set_enabled(freeze, False)
        trainer.trigger(refill)
        self.assertEqual(self._health(), fx.MAX_HEALTH)
        trainer.disconnect()

    def test_trainer_pauses_for_other_game(self):
        trainer = Trainer(self._table({"title_id": "0x4D530004", "cheats": [
            {"name": "Health", "type": "u16", "address": f"0x{HEALTH_VA:08X}", "value": 1}]}), pid=self.pid)
        trainer.set_enabled(trainer.table.cheats[0], True)
        trainer.tick()
        self.assertFalse(trainer.ready)
        self.assertIn("Another game", trainer.status)
        self.assertEqual(self._health(), fx.HEALTH)
        trainer.disconnect()

    def _unlock_table(self, **extra):
        return self._table({"cheats": [{"name": "Unlock", "type": "bytes", "mode": "once", "aob": fx.PATTERN,
                                        "value": fx.PATTERN + " 01 01 01", **extra}]})

    def test_aob_writes_block_and_ignores_stale_copy(self):
        trainer = Trainer(self._unlock_table(), pid=self.pid)
        cheat = trainer.table.cheats[0]
        trainer.tick()
        self.assertIsNone(trainer.current_value(cheat))
        trainer.trigger(cheat)
        self.assertIsNone(cheat.error)
        self.assertEqual(cheat.note, "OK")
        self.assertEqual(self.mem.read_phys(fx.SYSDATA_PA + 8, 4), b"\x01\x01\x01\x00")
        self.assertEqual(self.mem.read_phys(fx.STALE_PA + 8, 3), bytes(3))
        trainer.disconnect()

    def test_aob_refuses_ambiguous_match_unless_all_matches(self):
        second = fx.SYSDATA_PA + 0x1000
        self.mem.write_phys(second, fx.SYSDATA)
        trainer = Trainer(self._unlock_table(), pid=self.pid)
        cheat = trainer.table.cheats[0]
        trainer.tick()
        trainer.trigger(cheat)
        self.assertIn("found 2x", cheat.error)
        self.assertEqual(self.mem.read_phys(fx.SYSDATA_PA + 8, 1), b"\0")
        trainer.disconnect()

        trainer = Trainer(self._unlock_table(all_matches=True), pid=self.pid)
        cheat = trainer.table.cheats[0]
        trainer.tick()
        trainer.trigger(cheat)
        self.assertEqual(cheat.note, "OK (2x)")
        for pa in (fx.SYSDATA_PA, second):
            self.assertEqual(self.mem.read_phys(pa + 8, 3), b"\x01\x01\x01")
        trainer.disconnect()

    def test_aob_not_found(self):
        self.mem.write_phys(fx.SYSDATA_PA, bytes(len(fx.SYSDATA)))
        trainer = Trainer(self._unlock_table(), pid=self.pid)
        trainer.tick()
        trainer.trigger(trainer.table.cheats[0])
        self.assertIn("not found", trainer.table.cheats[0].error)
        trainer.disconnect()

    def test_shell_aob_and_set_bytes(self):
        shell = ScannerShell(self.mem, self._table({"cheats": []}))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            shell.onecmd("aob 3C 3C ?? 00 00 00 0A 14")
            shell.onecmd("set 0x80600010 bytes AA BB")
        text = out.getvalue()
        self.assertIn(f"VA 0x{0x80000000 + fx.SYSDATA_PA:08X}", text)
        self.assertIn("1 hit (+1 in free memory, ignored)", text)
        self.assertEqual(self.mem.read_phys(fx.SYSDATA_PA + 0x10, 2), b"\xAA\xBB")

    def test_inventory_set_add_remove_marks_checksum_group(self):
        ngb.protect(self.mem)
        self.assertEqual(self._slots(), [(190, 5), (193, 2)])
        ngb.set_count(self.mem, 193, 3)
        self.assertEqual(self._dirty_groups(), [0])
        ngb.set_count(self.mem, 251, 1)  # takes the slot the game freed
        self.assertEqual(self._slots(), [(190, 5), (251, 1), (193, 3)])
        ngb.set_count(self.mem, 190, 0)
        self.assertEqual(self._slots(), [(251, 1), (193, 3)])
        self.assertEqual(self.mem.read_phys(fx.state_pa(ngb.INVENTORY), 3), b"\xFF\xFF\x00")
        self.assertEqual(ngb.set_count(self.mem, 999, 0), [])
        with self.assertRaises(ValueError):
            ngb.set_count(self.mem, 251, 256)

    def test_inventory_full(self):
        self.mem.write_phys(fx.state_pa(ngb.INVENTORY), struct.pack("<HBB", 8, 1, 0) * ngb.SLOTS)
        with self.assertRaises(xbox.XboxError):
            ngb.set_count(self.mem, 251, 1)

    def test_write_hook_only_marks_touched_groups(self):
        ngb.protect(self.mem)
        self.mem.write(0x00589562, b"\x01\x02\x03\x04")  # karma (group 4) and the next dword (group 5)
        self.assertEqual(self._dirty_groups(), [4, 5])
        self.mem.write(HEALTH_VA, b"\x01\x00")
        self.assertEqual(self._dirty_groups(), [4, 5])

    def test_item_catalog(self):
        catalog = ngb.catalog(self.mem)
        self.assertEqual(catalog[190], ngb.ItemInfo(190, "Elixir of Spiritual Life", 10))
        self.assertEqual(catalog[247].name, "Plasma Saber Mk.II")
        self.assertEqual(catalog[251].label, "The Talisman of Rebirth (251)")
        self.assertEqual((catalog[193].name, catalog[193].limit), (None, 3))

    def test_trainer_item_freeze_and_inventory(self):
        trainer = Trainer(self._table({"title_id": "0x5443000D", "cheats": [
            {"name": "Talisman", "item": 251, "value": 1}]}), pid=self.pid)
        cheat = trainer.table.cheats[0]
        trainer.tick()
        self.assertTrue(trainer.inventory_ready, trainer.status)
        trainer.set_enabled(cheat, True)
        self.assertEqual(self._slots(), [(190, 5), (251, 1), (193, 2)])
        self.assertEqual(trainer.current_value(cheat), 1)
        # the game uses it up: id 0xFFFF, count 0 - but it is back on the next tick
        self.mem.write_phys(fx.state_pa(ngb.INVENTORY) + 4, b"\xFF\xFF\x00")
        trainer.tick()
        self.assertEqual(self._slots(), [(190, 5), (251, 1), (193, 2)])
        self.assertEqual(self._dirty_groups(), [0])
        trainer.set_enabled(cheat, False)

        trainer.freeze_item(190, 7)
        trainer.set_item(193, 3)
        self.mem.write_phys(fx.state_pa(ngb.INVENTORY) + 2, b"\x01")
        trainer.tick()
        self.assertEqual(self._slots(), [(190, 7), (251, 1), (193, 3)])
        trainer.set_item(190, 9)  # changes the frozen value as well
        trainer.tick()
        self.assertEqual(trainer.inventory()[0].count, 9)
        trainer.freeze_item(190, None)
        self.assertEqual(trainer.catalog()[251].limit, 3)
        trainer.disconnect()

    def test_shell_set_hit_with_type(self):
        shell = ScannerShell(self.mem, self._table({"cheats": []}))
        self._set_health(4321)
        with contextlib.redirect_stdout(io.StringIO()):
            shell.onecmd("scan u16 4321")
            nr = next(n for n in range(1, shell.scanner.count + 1) if shell.scanner.phys_address(n) == HEALTH_PA)
            shell.onecmd(f"set {nr} u16 777")  # a plain number is a hit, not address 0x1
        self.assertEqual(self._health(), 777)

    def test_item_names_while_loading(self):
        records = fx.state_pa(fx.NAME_RECORDS_VA)
        original = self.mem.read_phys(records, 12)
        try:
            self.mem.write_phys(records + 4, struct.pack("<I", 0x00FF0000))  # a name far off: no table yet
            self.assertEqual(ngb.catalog(self.mem), {})
        finally:
            self.mem.write_phys(records, original)

    def test_shell_set_karma_is_guarded(self):
        shell = ScannerShell(self.mem, self._table({"cheats": []}))
        with contextlib.redirect_stdout(io.StringIO()):
            shell.onecmd("set 0x00589560 u32 99999")
        self.assertEqual(self.mem.read_u32(0x00589560), 99999)
        self.assertEqual(self._dirty_groups(), [4])

    def test_shipped_essence_cheat_is_guarded(self):
        self.mem.write(ngb.ESSENCE, struct.pack("<I", 29837))
        trainer = Trainer(REPO / "cheats.json", pid=self.pid)
        essence = trainer.table.cheats[2]
        trainer.tick()
        trainer.set_enabled(essence, True)
        self.assertEqual(self.mem.read_u32(ngb.ESSENCE), ngb.ESSENCE_MAX)
        self.assertEqual(self._dirty_groups(), [6])
        self.mem.write_phys(fx.state_pa(ngb.ESSENCE), struct.pack("<I", 12345))  # bought something
        trainer.tick()
        self.assertEqual(trainer.current_value(essence), ngb.ESSENCE_MAX)
        trainer.disconnect()

    def test_shipped_ninpo_cheat_is_guarded(self):
        self.mem.write(ngb.NINPO_MAX, struct.pack("<HH", 1800, 1200))
        trainer = Trainer(REPO / "cheats.json", pid=self.pid)
        ninpo = trainer.table.cheats[3]
        trainer.tick()
        trainer.set_enabled(ninpo, True)
        self.assertEqual(trainer.current_value(ninpo), 1800)
        self.assertEqual(self._dirty_groups(), [19])
        self.mem.write_phys(fx.state_pa(ngb.NINPO), struct.pack("<H", 600))  # cast a ninpo
        trainer.tick()
        self.assertEqual(trainer.current_value(ninpo), 1800)
        trainer.disconnect()

    def test_shipped_ut_cheat_fills_the_charge(self):
        self.mem.write(ngb.UT_CHARGE, struct.pack("<HHH", 37, 100, 220))  # charging, steps 100 and 220
        trainer = Trainer(REPO / "cheats.json", pid=self.pid)
        ut = trainer.table.cheats[4]
        trainer.tick()
        trainer.set_enabled(ut, True)
        self.assertEqual(trainer.current_value(ut), 220)
        self.assertEqual(self._dirty_groups(), [])  # not checksummed
        self.mem.write_phys(fx.state_pa(ngb.UT_CHARGE), struct.pack("<H", 221))  # the game raised the level
        trainer.tick()
        self.assertEqual(trainer.current_value(ut), 220)
        trainer.disconnect()

    def test_worker_thread(self):
        trainer = Trainer(self._table({"title_id": "0x5443000D", "cheats": [
            {"name": "Health", "type": "u16", "address": "0x00010800", "offsets": ["0x40"], "value": 999,
             "hotkey": "CTRL+F9"},
            {"name": "Other", "type": "u16", "address": "0x00400000", "value": 1, "hotkey": "F9"}]}), pid=self.pid)
        worker = TrainerThread(trainer)
        with mock.patch("ngbtrainer.worker.beep"):
            worker.start()
            try:
                wait_for(lambda: worker.snapshot.ready)
                snap = worker.snapshot
                self.assertEqual(snap.hotkey_warnings, ("CTRL+F9 switches “Other” (F9) as well.", None))
                self.assertEqual([s.item for s in snap.slots], [190, 193])
                worker.toggle(snap.table[0])
                wait_for(lambda: worker.snapshot.cheats[0].enabled)
                self._set_health(5)
                wait_for(lambda: self._health() == 999)  # the freeze loop runs in the thread

                worker.set_item(251, 2, done=lambda error: None)
                done, error = worker.results.get(timeout=5)
                self.assertIsNone(error)
                self.assertIn((251, 2), [(s.item, s.count) for s in worker.snapshot.slots])
                worker.set_item(251, 300, done=lambda error: None)
                self.assertIsInstance(worker.results.get(timeout=5)[1], ValueError)

                worker.toggle(snap.table[0])
                wait_for(lambda: not worker.snapshot.cheats[0].enabled)
                self._set_health(4321)
                worker.scan_first("u16", "4321", done=lambda error: None)
                self.assertIsNone(worker.results.get(timeout=10)[1])
                scan = worker.snapshot.scan
                self.assertEqual((scan.kind, scan.vtype, scan.listed), ("value", "u16", True))
                n = next(n for n, hit in enumerate(scan.hits) if hit.pa == HEALTH_PA)
                self.assertEqual(scan.values[n], 4321)
                worker.scan_reset()
                wait_for(lambda: worker.snapshot.scan.kind is None)
            finally:
                worker.stop()
        self.assertIsNone(trainer.mem)  # disconnected on the way out

    def test_scan_session(self):
        session = ScanSession(self.mem, xbox.read_xbe_info(self.mem))
        self._set_health(4321)
        session.first("u16", None)  # unknown value: stored, nothing listed yet
        self.assertFalse(session.listed)
        self._set_health(4000)
        session.next("<")
        hit = next(h for h in session.hits if h.pa == HEALTH_PA)
        self.assertEqual((hit.va, hit.static, hit.previous), (HEALTH_VA, False, 4000))
        session.next("=", "4000")
        self.assertEqual(session.values()[session.hits.index(hit)], 4000)
        session.write(hit, "123")
        self.assertEqual(self._health(), 123)
        session.next("-=", "3877")
        with self.assertRaises(ScanError):  # the hit of an earlier search
            session.write(hit, "1")

        hit = next(h for h in session.hits if h.pa == HEALTH_PA)
        session.search_pointers(hit)
        pointer = next(p for p in session.pointers if p.va == fx.PLAYER_POINTER_VA)
        self.assertEqual((pointer.offset, pointer.static), (fx.HEALTH_OFFSET, True))
        self.assertTrue(session.pointers[0].static)
        session.follow(pointer)
        self.assertEqual((session.chain, session.target), ((fx.HEALTH_OFFSET,), HEALTH_VA))

        session.search_pattern(fx.PATTERN)
        self.assertEqual((session.count, session.stale), (1, 1))
        self.assertEqual(session.hits[0].va, 0x80000000 + fx.SYSDATA_PA)
        with self.assertRaises(ScanError):
            session.next("=")

    def test_unmapped_pointer_reports_error(self):
        trainer = Trainer(self._table({"cheats": [
            {"name": "Broken", "type": "u32", "address": "0x00900000", "offsets": [4], "value": 1}]}), pid=self.pid)
        cheat = trainer.table.cheats[0]
        trainer.tick()
        trainer.set_enabled(cheat, True)
        self.assertIn("not mapped", cheat.error)
        trainer.disconnect()


class OfflineTest(unittest.TestCase):
    def test_hotkey_parsing(self):
        self.assertEqual(parse_keys("ctrl+F1"), (0x11, 0x70))
        self.assertEqual(parse_pad("BACK+DPAD_UP"), 0x21)
        self.assertEqual(parse_pad("LT+RT"), 0x30000)
        for bad in ("F99", " ", "+"):
            with self.assertRaises(ValueError, msg=bad):
                parse_keys(bad)
        for bad in ("BACK+Z", ""):
            with self.assertRaises(ValueError, msg=bad):
                parse_pad(bad)

    def test_hotkey_overlaps(self):
        hotkeys = Hotkeys()
        hotkeys.bind("a", "F1")
        hotkeys.bind("b", "ctrl+F1")
        hotkeys.bind("c", "F1", "BACK")
        hotkeys.bind("d", "F2", "back+dpad_up")
        found = {(o.payload, o.combo, o.other, o.other_combo) for o in hotkeys.overlaps()}
        self.assertEqual(found, {("a", "F1", "c", "F1"), ("c", "F1", "a", "F1"), ("b", "CTRL+F1", "a", "F1"),
                                 ("b", "CTRL+F1", "c", "F1"), ("d", "Pad BACK+DPAD_UP", "c", "Pad BACK")})

    def test_hotkeys_pause_for_keyboard(self):
        pressed = set()
        user32 = types.SimpleNamespace(GetAsyncKeyState=lambda vk: 0x8000 if vk in pressed else 0)
        hotkeys = Hotkeys()
        hotkeys.bind("a", "F1")
        with mock.patch("ngbtrainer.hotkeys._user32", user32):
            pressed.add(0x70)
            self.assertEqual(hotkeys.poll(), [])  # was down while binding
            pressed.clear()
            hotkeys.poll()
            pressed.add(0x70)
            self.assertEqual(hotkeys.poll(keyboard=False), [])
            self.assertEqual(hotkeys.poll(), [])  # pressed during the pause: must be released first
            pressed.clear()
            hotkeys.poll()
            pressed.add(0x70)
            self.assertEqual(hotkeys.poll(), ["a"])

    def test_table_validation(self):
        path = Path(tempfile.mkdtemp()) / "cheats.json"
        for bad in ({"name": "a", "type": "u9", "address": "0x1"},
                    {"name": "b", "type": "u8", "address": "0x1", "value": 300},
                    {"name": "c", "type": "u8", "address": "0x1", "mode": "once"},
                    {"name": "d", "type": "u8"},
                    {"name": "e", "type": "bytes", "address": "0x1"},
                    {"name": "f", "type": "u8", "aob": "3C", "value": 1},
                    {"name": "g", "type": "u8", "aob": "3C", "address": "0x1", "value": 1, "mode": "once"},
                    {"name": "h", "type": "bytes", "aob": "3G", "value": "01", "mode": "once"},
                    {"name": "i", "type": "bytes", "address": "0x1", "value": "0"},
                    {"name": "j", "item": 251, "address": "0x1"},
                    {"name": "k", "item": 251, "type": "u16", "value": 1},
                    {"name": "l", "item": 251, "value": 256},
                    {"name": "m", "item": 251, "mode": "once"},
                    {"name": "n", "type": "u16", "address": "0x1", "value": 99.5},
                    {"name": "o", "type": "u8", "address": "0x1", "value": True}):
            path.write_text(json.dumps({"cheats": [bad]}), encoding="utf-8")
            with self.assertRaises(ValueError, msg=bad) as ctx:
                CheatTable.load(path)
            self.assertIn(f"Cheat '{bad['name']}'", str(ctx.exception))
        with self.assertRaises(ValueError):
            add_to_table(path, {"name": "e", "type": "u8", "address": "0x1", "value": -1}, None)

    def test_combo_recorder(self):
        pressed = {0x70}  # F1, held when recording starts
        user32 = types.SimpleNamespace(GetAsyncKeyState=lambda vk: 0x8000 if vk in pressed else 0)
        with mock.patch("ngbtrainer.hotkeys._user32", user32):
            recorder = ComboRecorder()
            self.assertIsNone(recorder.poll())
            pressed.clear()
            self.assertIsNone(recorder.poll())  # only let go: nothing recorded
            for change in (lambda: pressed.add(0x70), lambda: pressed.add(0x11), lambda: pressed.discard(0x70)):
                change()
                self.assertIsNone(recorder.poll())
            pressed.clear()
            self.assertEqual(recorder.poll(), "CTRL+F1")  # modifiers first
        with mock.patch("ngbtrainer.hotkeys._XInput", FakePad):
            FakePad.mask = 0
            recorder = ComboRecorder(pad=True)
            for mask in (PAD["DPAD_UP"], PAD["DPAD_UP"] | PAD["BACK"]):
                FakePad.mask = mask
                self.assertIsNone(recorder.poll())
            FakePad.mask = 0
            self.assertEqual(recorder.poll(), "BACK+DPAD_UP")

    def test_hotkeys_pause_for_controller(self):
        with mock.patch("ngbtrainer.hotkeys._XInput", FakePad):
            FakePad.mask = 0
            hotkeys = Hotkeys()
            hotkeys.bind("a", pad="BACK")
            hotkeys.poll()
            FakePad.mask = PAD["BACK"]
            self.assertEqual(hotkeys.poll(pad=False), [])
            self.assertEqual(hotkeys.poll(), [])  # pressed during the pause: must be released first
            FakePad.mask = 0
            hotkeys.poll()
            FakePad.mask = PAD["BACK"]
            self.assertEqual(hotkeys.poll(), ["a"])

    def test_table_edit(self):
        path = Path(tempfile.mkdtemp()) / "cheats.json"
        add_to_table(path, {"name": "a", "type": "u8", "address": "0x1"}, 0x5443000D)
        add_to_table(path, {"name": "b", "type": "u8", "address": "0x2"}, None)
        self.assertEqual(table_entry(path, 1, "b")["address"], "0x2")
        replace_in_table(path, 0, "a", {"name": "c", "type": "u8", "address": "0x3"})
        replace_in_table(path, 1, "b", None)
        self.assertEqual([c.name for c in CheatTable.load(path).cheats], ["c"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["title_id"], "0x5443000D")
        with self.assertRaises(ValueError):  # the file changed since: cheat 0 is no longer "a"
            replace_in_table(path, 0, "a", None)
        with self.assertRaises(ValueError):  # invalid cheats are not written
            replace_in_table(path, 0, "c", {"name": "c", "type": "u9", "address": "0x1"})
        self.assertEqual(CheatTable.load(path).cheats[0].type, "u8")

    def test_editor_form_round_trip(self):
        labels = {251: "The Talisman of Rebirth (251)"}
        ids = {label: item for item, label in labels.items()}
        shipped = json.loads((REPO / "cheats.json").read_text(encoding="utf-8"))["cheats"]
        for entry in [*shipped,
                      {"name": "Health", "type": "u16", "address": "0x00ABCDE0", "offsets": ["0x84"],
                       "copy_from": {"address": "0x00ABCDE0", "offsets": ["0x86"]}, "hotkey": "F1",
                       "pad": "BACK+DPAD_UP"},
                      {"name": "Essence", "type": "u32", "address": "0xD0012340", "value": 99999, "mode": "once",
                       "hotkey": "CTRL+F2"},
                      {"name": "Tempo", "type": "f32", "address": "0x00012340", "value": 1.5, "note": "kept"}]:
            form = form_from_entry(entry, labels)
            self.assertEqual(entry_from_form(form, ids, entry), entry, msg=entry["name"])
        self.assertEqual(form_from_entry(shipped[1], labels).item, "The Talisman of Rebirth (251)")
        self.assertEqual(entry_from_form(Form(name=" x ", address="10", offsets="0x84 0x10", value="0x10",
                                              hotkey="ctrl + f1"), ids),
                         {"name": "x", "type": "u32", "address": "0x0000000A", "offsets": ["0x84", "0x10"],
                          "value": 16, "hotkey": "CTRL+F1"})

    def test_editor_form_errors(self):
        for form, message in ((Form(), "name"),
                              (Form(name="x", address="zz"), "not an address"),
                              (Form(name="x", address="0x10", value="abc"), "not a number"),
                              (Form(name="x", address="0x10", type="u8", value="300"), "does not fit"),
                              (Form(name="x", address="0x10", value="1", hotkey="F99"), "F99"),
                              (Form(name="x", target="item", item="No such item", value="1"), "Item"),
                              (Form(name="x", target="aob", aob="3G"), "3G")):
            with self.assertRaises(ValueError, msg=form) as ctx:
                entry_from_form(form, {})
            self.assertIn(message, str(ctx.exception))

    def test_table_file_format(self):
        path = Path(tempfile.mkdtemp()) / "cheats.json"
        entry = {"name": "a", "type": "u8", "address": "0x1"}
        path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"cheats": [entry]}).encode())  # BOM, as some editors save
        self.assertEqual(len(CheatTable.load(path).cheats), 1)
        add_to_table(path, {**entry, "name": "b"}, None)
        self.assertEqual([c.name for c in CheatTable.load(path).cheats], ["a", "b"])
        self.assertEqual(CheatTable.load(path).cheats[0].value, None)
        self.assertEqual(CheatTable.load(path).cheats[0].target.address, 1)
        for bad in ([entry], {"cheats": {"a": entry}}, {"cheats": ["a"]}):
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError, msg=bad):
                CheatTable.load(path)

    def test_byte_pattern(self):
        pattern = BytePattern.parse("3c3c??")
        self.assertEqual(pattern.text, "3C 3C ??")
        self.assertEqual(pattern.find(b"\x00\x3C\x3C\x99\x3C\x3C"), [1])

    def test_shipped_table_is_valid(self):
        table = CheatTable.load(Path(__file__).resolve().parent.parent / "cheats.json")
        unlock, talisman, essence, ninpo, ut = table.cheats
        self.assertEqual((unlock.type, unlock.mode, unlock.aob.text, unlock.all_matches),
                         ("bytes", "once", fx.PATTERN, True))
        self.assertEqual(len(unlock.value), 624)
        self.assertTrue(unlock.value.startswith(bytes.fromhex(fx.PATTERN)))
        self.assertEqual((talisman.item, talisman.value, talisman.mode), (251, 1, "freeze"))
        self.assertEqual((essence.type, essence.target.address, essence.value, essence.mode),
                         ("u32", ngb.ESSENCE, ngb.ESSENCE_MAX, "freeze"))
        self.assertEqual((ninpo.type, ninpo.target.address, ninpo.source.address, ninpo.mode),
                         ("u16", ngb.NINPO, ngb.NINPO_MAX, "freeze"))
        self.assertEqual((ut.type, ut.target.address, ut.source.address, ut.mode),
                         ("u16", ngb.UT_CHARGE, ngb.UT_STEP2, "freeze"))

    def test_missing_table_is_empty(self):
        table = CheatTable.load(Path(tempfile.mkdtemp()) / "missing.json")
        self.assertEqual((table.title_id, table.cheats), (None, []))


if __name__ == "__main__":
    unittest.main()
