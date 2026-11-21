from ostinato.backends.fm1_sloop import protocol as P


def test_v14_roundtrip():
    for v in (-8192, -64, -1, 0, 1, 63, 127, 8191):
        assert P.v14dec(*P.v14enc(v)) == v
    assert P.v14enc(0) == [0, 64]
    assert P.v14enc(99999) == P.v14enc(8191)


def test_pack7_roundtrip():
    data = bytes(range(256))[:23]
    data = bytes([0xFF, 0x80, 0x7F, 0x00, 0x81, 0xAA, 0x55, 0x01, 0xFE])
    packed = P.pack7(data)
    assert all(0 <= b <= 0x7F for b in packed)
    assert P.unpack7(packed) == data
    assert len(P.pack7(bytes(16))) == 19  # FILL_GET: 16 bytes -> 19 on the wire


def test_frame_unframe():
    f = P.frame(P.Cmd.TRACK_STEP, [2, 5])
    assert f == [0xF0, 0x7D, 0x46, 0x4C, 30, 2, 5, 0xF7]
    assert P.unframe(f) == (30, [2, 5])
    assert P.unframe([0xF0, 0x43, 0x00, 0x00, 0x01, 0xF7]) is None  # someone else's SysEx
    try:
        P.frame(1, [128])
    except ValueError:
        pass
    else:
        raise AssertionError("8-bit data byte accepted")


def test_reply_matching():
    assert P.reply_matches(P.Cmd.TRACK_STEP, [1, 7], [1, 7, 0])
    assert not P.reply_matches(P.Cmd.TRACK_STEP, [1, 7], [1, 8, 0])
    assert P.reply_matches(P.Cmd.WATCH, [3], [1])  # v3 firmware answers 1 to WATCH 3
    assert not P.reply_matches(P.Cmd.WATCH, [1], [0])


def test_step_parse_with_levels():
    # index 3, 2 notes C4 E4, NOTE, accent, vel 90, lvl: note0 HARD(3) note1 GHOST(1) -> 0b0111 = 7, rat note0 x2 -> 1
    a = [3, 2, 60, 64, 0, 0, 0, 1, 90, 7, 0, 1]
    s = P.parse_step(a)
    assert s.active_notes == [60, 64]
    assert s.note_level(0) == 3 and s.note_level(1) == 1
    assert s.note_ratchet(0) == 2 and s.note_ratchet(1) == 1
    assert s.flags & P.FLAG_ACCENT


def test_drum_step_parse():
    # lanes 0 (kick, HARD) and 4 (hat, ratchet x3)
    on = (1 << 0) | (1 << 4)
    lv = 3 << 0
    rt = 2 << 8
    g7 = lambda x, n: [(x >> (7 * k)) & 0x7F for k in range(n)]
    d = P.parse_drum_step([5, *g7(on, 3), *g7(lv, 5), *g7(rt, 5)])
    assert d.index == 5 and d.lanes() == [0, 4]
    assert d.lvl[0] == 3 and d.rat[4] == 2
    s = P.drum_as_step(d)
    assert s.active_notes == [36, 42] and s.flags & P.FLAG_ACCENT


def test_fill_of():
    raw = [0] * 16
    raw[1] = 0b10_00_01_00  # step 5: 1 (fill only), step 7: 2 (no fill)
    assert P.fill_of(raw, 5) == 1 and P.fill_of(raw, 7) == 2 and P.fill_of(raw, 4) == 0
    raw[0] = 0b11  # 3 reads as normal
    assert P.fill_of(raw, 0) == 0


def test_note_names():
    assert P.note_name(60) == "C4" and P.note_name(45) == "A2" and P.note_name(61) == "C#4"


def test_fmt_values():
    d = P.Desc(0, 0, P.Fmt.DB, 0, 127, 104, "LVL", "")
    assert P.fmt_value(d, 112) == "0.0dB" and P.fmt_value(d, 0) == "OFF"
    e = P.Desc(0, 1, P.Fmt.ENUM, 0, 2, 0, "QNT", "", ["OFF", "SNAP", "WHITE"])
    assert P.fmt_value(e, 2) == "WHITE"
    assert P.fmt_value(P.Desc(0, 2, P.Fmt.BIPCT, -64, 63, 0, "PAN", ""), -32) == "-50%"
