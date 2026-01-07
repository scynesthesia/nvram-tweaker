import nvram_parsing
import nvram_reconstruction


def test_rebuild_text_round_trips_separators() -> None:
    content = (
        "Preamble\n"
        "Setup Question = First\n"
        "Value = <1>\n"
        "\n"
        "Setup Question = Second\n"
        "Options =*[00]Off\n"
    )

    blocks, trailing_text = nvram_parsing.find_blocks(content)
    rebuilt = nvram_reconstruction.rebuild_text(blocks, trailing_text=trailing_text)

    assert rebuilt == content
