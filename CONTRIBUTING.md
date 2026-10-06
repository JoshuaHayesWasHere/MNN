# Contributing to MNN

Thank you for wanting to help. MNN is a small project with a narrow job: get
a paper your agent wrote onto an e-reader every morning, and never leave the
reader with a broken device. Changes that make that more reliable, or open it
to more agents and more devices, are the ones most likely to be merged.

## The most useful things you can do

- **Try it on your device and say how it went.** The Kindle side has run on
  one Paperwhite 11. A report from any other model or firmware, working or
  not, is worth more than most code. Use the *Device report* issue form.
- **Connect another agent.** The inbox and staging work with anything that
  can write a file or make an HTTP request. A short write-up of how you
  pointed your agent at them helps the next person.
- **Fix something in the [limitations](README.md#limitations).**
- **Improve the docs.** If a step confused you, it will confuse someone else.

For anything larger than a fix, open an issue first so we can agree on the
shape before you spend the time.

## Getting set up

You need [uv](https://docs.astral.sh/uv/) for the tests and Docker to run the
Press.

```sh
git clone https://github.com/JoshuaHayesWasHere/MNN.git && cd MNN
make test      # run the tests; nothing reaches the network
make check     # the tests plus shellcheck, as CI runs them
make sample    # build the sample edition into out/
make up        # build and start the whole Press
make help      # everything else
```

`make check` needs [shellcheck](https://www.shellcheck.net/) 0.10 or later,
because the Kindle's scripts are checked as BusyBox `sh`.

## Finding your way around

[docs/press.md](docs/press.md#what-is-in-the-repository) has a map of the
repository. In short:

| Where | What |
| --- | --- |
| `src/mnn/` | The Press: sources, the builder, the server, the staging tools |
| `src/mnn/sources/` | The built-in sources. A new kind of section starts here |
| `kindle/` | What the Kindle runs, in POSIX `sh` for BusyBox |
| `integrations/` | Agent-specific pieces |
| `tests/` | One file per part; `tests/test_kindle.py` runs the Kindle scripts against a stand-in Kindle |

## What a good change looks like

- **It has a test.** Feeds come from `tests/fixtures`, the Kindle from the
  stand-in in `tests/test_kindle.py`, and an agent from the fake in
  `tests/fakes.py`. A test never reaches the network.
- **It fails safe.** A source that breaks loses its own section. A print that
  breaks leaves yesterday's paper in place. A Kindle script that breaks falls
  back to the last one that worked. New code keeps those promises.
- **It says what was not tested.** Most of us do not own every device. If you
  could not try something on real hardware, say so in the pull request and in
  the docs, the way the existing docs do.
- **It keeps the docs true.** If a command, a setting or a file moves, the
  page that mentions it moves with it.
- **It adds no dependency lightly.** The image carries the standard library,
  Pillow and ebooklib. `bin/mnn` and the staging tools use the standard
  library only, on purpose.

## Pull requests

- Branch from `main` and open a pull request; `main` takes no direct pushes.
- Title it the way the history reads: `feat(kindle): ...`, `fix(sources): ...`,
  `docs: ...`, `refactor: ...`, `ci: ...`. Pull requests are squashed, so the
  title becomes the commit.
- The `check` job has to pass: shellcheck, a byte-compile, and the tests.
- Fill in the template. "Checked" and "Not checked" matter more than a long
  description.

## Reporting a security problem

Please do not open a public issue. See [SECURITY.md](SECURITY.md).

## Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

## Licence

By contributing you agree that your work is released under the
[MIT licence](LICENSE) that covers the project.
