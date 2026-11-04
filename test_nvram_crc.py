import nvram_crc


def test_crc_insertion_preserves_header() -> None:
    content = "Setup Question = First\nValue = <1>\n"
    initial = "HIICrc32=seed"

    rewritten = nvram_crc.preserve_crc_header(content, initial)

    assert rewritten.splitlines()[0] == initial


def test_header_with_scewin_spacing_is_kept_verbatim() -> None:
    header = "HIICrc32= 5B2C3F1A  "
    content = f"{header}\nSetup Question = First\nValue = <1>\n"

    assert nvram_crc.extract_crc_header(content) == header
    assert nvram_crc.preserve_crc_header(content, header) == content


def test_changed_header_is_restored() -> None:
    content = "HIICrc32= 00000000\nSetup Question = First\nValue = <1>\n"

    rewritten = nvram_crc.preserve_crc_header(content, "HIICrc32= 5B2C3F1A")

    assert rewritten == "HIICrc32= 5B2C3F1A\nSetup Question = First\nValue = <1>\n"


def test_header_mismatch_detection() -> None:
    original = "HIICrc32= 5B2C3F1A\nSetup Question = First\n"
    same = "HIICrc32= 5B2C3F1A\nSetup Question = First\nValue = <2>\n"
    other_bios = "HIICrc32= 11111111\nSetup Question = First\n"
    missing = "Setup Question = First\n"

    assert nvram_crc.crc_header_mismatch(original, same) is None
    assert "different BIOS version" in nvram_crc.crc_header_mismatch(original, other_bios)
    assert "missing" in nvram_crc.crc_header_mismatch(original, missing)
    assert nvram_crc.crc_header_mismatch(missing, other_bios) is None
