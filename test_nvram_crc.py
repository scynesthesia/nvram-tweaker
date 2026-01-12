import nvram_crc


def test_crc_insertion_preserves_header() -> None:
    content = "Setup Question = First\nValue = <1>\n"
    initial = "HIICrc32=seed"

    rewritten = nvram_crc.recalculate_crc(content, initial)

    assert rewritten.splitlines()[0] == initial


def test_crc_bypass_modes() -> None:
    content = "HIICrc32=old\nSetup Question = First\nValue = <1>\n"
    placeholder = nvram_crc.recalculate_crc(content, "HIICrc32=orig,ver=02", bypass=True, bypass_mode="placeholder")
    first_line = placeholder.splitlines()[0]
    assert "<bypassed by nvram-tweaker" in first_line
    assert first_line.endswith(",ver=02")

    removed = nvram_crc.recalculate_crc(content, "HIICrc32=orig", bypass=True, bypass_mode="remove")
    assert not removed.startswith("HIICrc32=")
