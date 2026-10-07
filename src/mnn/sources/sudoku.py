"""A built-in source: the day's sudoku, and its solution on the next page.

    [[section]]
    title = "Puzzles"
    source = "sudoku"
    difficulty = "medium"

`difficulty` is `easy`, `medium` (the default) or `hard`. The puzzle is made
from the date, so printing the same day's paper again prints the same puzzle,
and every puzzle has exactly one solution.

There is no writing on the page: an e-reader shows the grid, and the solving
is done on paper or in your head. Standard library only.
"""

from __future__ import annotations

import datetime as dt
import random

SIDE = 9
BOX = 3
DIGITS = "123456789"
# The fewest clues the maker aims for. It stops sooner when taking another
# pair of clues away would leave more than one solution.
CLUES = {"easy": 40, "medium": 32, "hard": 26}
DEFAULT_DIFFICULTY = "medium"
RULES = ("Fill the grid so that every row, every column and every box of "
         "three by three holds each digit from 1 to 9 once.")


def solved(rng: random.Random) -> list[list[int]]:
    """A full, valid grid: a fixed pattern with its digits, and its rows and
    columns within and between the bands, shuffled."""
    def order() -> list[int]:
        bands = rng.sample(range(BOX), BOX)
        return [band * BOX + line for band in bands for line in rng.sample(range(BOX), BOX)]

    rows, columns, digits = order(), order(), rng.sample(range(1, SIDE + 1), SIDE)
    return [[digits[(BOX * (r % BOX) + r // BOX + c) % SIDE] for c in columns] for r in rows]


def _candidates(grid: list[list[int]], r: int, c: int) -> list[int]:
    seen = set(grid[r]) | {grid[i][c] for i in range(SIDE)}
    top, left = r - r % BOX, c - c % BOX
    seen |= {grid[i][j] for i in range(top, top + BOX) for j in range(left, left + BOX)}
    return [digit for digit in range(1, SIDE + 1) if digit not in seen]


def solutions(grid: list[list[int]], limit: int = 2) -> int:
    """How many ways the grid can be completed, counting no further than
    `limit`. The cell with the fewest candidates is always tried first."""
    best, best_candidates = None, None
    for r in range(SIDE):
        for c in range(SIDE):
            if grid[r][c]:
                continue
            candidates = _candidates(grid, r, c)
            if not candidates:
                return 0
            if best_candidates is None or len(candidates) < len(best_candidates):
                best, best_candidates = (r, c), candidates
    if best is None:
        return 1
    r, c = best
    count = 0
    for digit in best_candidates:
        grid[r][c] = digit
        count += solutions(grid, limit - count)
        grid[r][c] = 0
        if count >= limit:
            break
    return count


def puzzle(solution: list[list[int]], clues: int, rng: random.Random) -> list[list[int]]:
    """The solution with clues taken away in pairs, each cell with the one
    opposite it through the centre, for as long as one solution remains."""
    grid = [row[:] for row in solution]
    cells = [(r, c) for r in range(SIDE) for c in range(SIDE) if (r, c) <= (SIDE - 1 - r, SIDE - 1 - c)]
    rng.shuffle(cells)
    left = SIDE * SIDE
    for r, c in cells:
        pair = {(r, c), (SIDE - 1 - r, SIDE - 1 - c)}
        if left - len(pair) < clues:
            continue
        kept = {cell: grid[cell[0]][cell[1]] for cell in pair}
        for row, column in pair:
            grid[row][column] = 0
        if solutions(grid) == 1:
            left -= len(pair)
        else:
            for (row, column), digit in kept.items():
                grid[row][column] = digit
    return grid


def rows(grid: list[list[int]]) -> list[str]:
    return ["".join(str(cell) if cell else "." for cell in row) for row in grid]


def make(date: dt.date, difficulty: str) -> tuple[list[str], list[str]]:
    """(puzzle, solution) for a day, as the rows an edition holds."""
    rng = random.Random(f"mnn-sudoku:{date.isoformat()}:{difficulty}")
    solution = solved(rng)
    return rows(puzzle(solution, CLUES[difficulty], rng)), rows(solution)


def produce(date: dt.date, config: dict) -> dict:
    difficulty = config.get("difficulty", DEFAULT_DIFFICULTY)
    if difficulty not in CLUES:
        raise ValueError("'difficulty' must be one of: " + ", ".join(CLUES))
    grid, solution = make(date, difficulty)
    clues = sum(cell != "." for row in grid for cell in row)
    return {
        "title": config.get("title", ""),
        "articles": [
            {"title": "Sudoku",
             "deck": RULES,
             "body": [f"{difficulty.capitalize()}, with {clues} clues. "
                      "The solution is on the next page."],
             "grid": grid},
            {"title": "Sudoku: the solution",
             "body": [f"The solution to the puzzle for {date:%A}, {date:%B} {date.day}."],
             "grid": solution},
        ],
    }
