"""The sudoku source, and the grid an edition can carry."""

from __future__ import annotations

import datetime as dt
import json
import zipfile

import pytest

from mnn import build_paper, press_sources
from mnn.sources import sudoku

DATE = dt.date(2026, 10, 6)
DIGITS = set("123456789")


def groups(rows: list[str]) -> list[str]:
    columns = ["".join(row[c] for row in rows) for c in range(9)]
    boxes = ["".join(rows[r + i][c + j] for i in range(3) for j in range(3))
             for r in (0, 3, 6) for c in (0, 3, 6)]
    return [*rows, *columns, *boxes]


@pytest.mark.parametrize("difficulty", ["easy", "medium", "hard"])
@pytest.mark.parametrize("day", range(5))
def test_every_puzzle_is_valid_and_has_one_solution(difficulty, day):
    grid, solution = sudoku.make(DATE + dt.timedelta(days=day), difficulty)
    assert all(set(group) == DIGITS for group in groups(solution))
    # The clues are the solution's own digits, and they allow no other.
    assert all(clue in (".", digit) for clues, digits in zip(grid, solution)
               for clue, digit in zip(clues, digits))
    cells = [[int(cell) if cell != "." else 0 for cell in row] for row in grid]
    assert sudoku.solutions(cells) == 1
    # Opposite cells are given or empty together.
    flat = "".join(grid)
    assert all((flat[i] == ".") == (flat[80 - i] == ".") for i in range(81))


def test_harder_puzzles_give_fewer_clues():
    def clues(difficulty: str) -> int:
        return sum(cell != "." for row in sudoku.make(DATE, difficulty)[0] for cell in row)

    assert clues("easy") >= 40 > clues("medium") >= 32 >= clues("hard") >= 26


def test_a_day_always_gets_the_same_puzzle_and_days_differ():
    assert sudoku.make(DATE, "medium") == sudoku.make(DATE, "medium")
    assert sudoku.make(DATE, "medium") != sudoku.make(DATE + dt.timedelta(days=1), "medium")
    assert sudoku.make(DATE, "medium")[1] != sudoku.make(DATE, "hard")[1]


def test_the_section_is_the_puzzle_then_its_solution():
    section = sudoku.produce(DATE, {"title": "Puzzles"})
    puzzle, solution = section["articles"]
    assert puzzle["title"] == "Sudoku" and "next page" in puzzle["body"][0]
    assert "Medium, with" in puzzle["body"][0]
    assert any("." in row for row in puzzle["grid"])
    assert solution["title"] == "Sudoku: the solution"
    assert "Tuesday, October 6" in solution["body"][0]
    assert not any("." in row for row in solution["grid"])


def test_an_unknown_difficulty_fails_the_section():
    with pytest.raises(ValueError, match="'difficulty' must be one of: easy, medium, hard"):
        sudoku.produce(DATE, {"difficulty": "fiendish"})


@pytest.mark.parametrize("grid", [
    ["123456789"] * 8,
    ["12345678"] + ["123456789"] * 8,
    ["12345678x"] + ["123456789"] * 8,
    ["1234567 9"] + ["123456789"] * 8,
    "123456789",
    [123456789] * 9,
])
def test_a_grid_that_is_not_nine_by_nine_is_refused(grid):
    with pytest.raises(build_paper.EditionError, match="'grid' must be 9 rows of 9 cells"):
        build_paper.parse_section(
            {"title": "Puzzles", "articles": [{"title": "Sudoku", "body": ["x"], "grid": grid}]},
            "section")


def test_the_press_carries_the_grid_into_the_paper(config_dir, tmp_path):
    (config_dir / "sources.toml").write_text(
        '[[section]]\ntitle = "Puzzles"\nsource = "sudoku"\ndifficulty = "easy"\n')
    paper = press_sources.load_config(config_dir)
    sections, outcomes = press_sources.run_sources(paper, DATE, config_dir)
    assert outcomes[0]["status"] == "ok"
    grid = sections[0]["articles"][0]["grid"]
    assert grid == sudoku.make(DATE, "easy")[0]

    edition = press_sources.assemble(paper, DATE, sections)
    path = tmp_path / "edition.json"
    path.write_text(json.dumps(edition))
    epub = build_paper.write_epub(build_paper.load_edition(path), tmp_path)
    with zipfile.ZipFile(epub) as book:
        pages = "".join(book.read(name).decode() for name in book.namelist()
                        if name.endswith(".xhtml"))
    assert pages.count('<table class="grid">') == 2
    assert pages.count("<td") >= 2 * 81
    # The first row's clues are in the page, and its empty cells are empty.
    first = grid[0].replace(".", "")
    assert all(f">{digit}</td>" in pages for digit in first)
    assert "&#160;</td>" in pages or " </td>" in pages
    # A puzzle's byline says what it is, not how long it takes to read.
    assert "Puzzle</p>" in pages and "minute read" not in pages
