"""Nox automation definitions."""

import pathlib

import nox

nox.options.sessions = ["dev"]
nox.options.reuse_existing_virtualenvs = True


@nox.session(python="3.10")
def dev(session: nox.Session) -> None:
    """Create a dev environment with everything installed.

    This is useful for setting up IDE for autocompletion etc. Point the
    development environment to ``.nox/dev``.
    """
    env = {
        "CFLAGS": "-I$(brew --prefix openssl)/include",
        "LDFLAGS": "-L$(brew --prefix openssl)/lib -L/usr/local/opt/openssl/lib",
        "CPPFLAGS": "-I$(brew --prefix openssl)/include"
    }
    session.install("nox")
    session.install("--pre", "--no-binary", ":all:", "pymssql", "--no-cache", env=env)
    session.install("-e", ".[all,tests]")


@nox.session(python=["3.8", "3.9", "3.10", "3.11", "3.12"])
@nox.parametrize("airflow", ["3.0.1", "3.0.2"])
def test(session: nox.Session, airflow) -> None:
    """Run both unit and integration tests."""
    env = {
        "AIRFLOW_HOME": f"~/airflow-{airflow}-python-{session.python}",
        "AIRFLOW__CORE__ALLOWED_DESERIALIZATION_CLASSES": "airflow.* astro.*",
    }

    session.install(f"apache-airflow~={airflow}")
    session.install("-e", ".[all,tests]")

    # Log all the installed dependencies
    session.log("Installed Dependencies:")
    session.run("pip3", "freeze")

    session.run("airflow", "db", "init", env=env)

    # Since pytest is not installed in the nox session directly, we need to set `external=true`.
    session.run(
        "pytest",
        "-vv",
        *session.posargs,
        env=env,
        external=True,
    )


@nox.session(python=["3.10"])
def type_check(session: nox.Session) -> None:
    """Run MyPy checks."""
    session.install("-e", ".[all,tests]")
    session.run("mypy", "--version")
    session.run("mypy")


@nox.session()
@nox.parametrize(
    "extras",
    [
        ("postgres-amazon", {"include": ["postgres", "amazon"]}),
        ("snowflake-amazon", {"include": ["snowflake", "amazon"]}),
        ("sqlite", {"include": ["sqlite"]}),
    ],
)
def test_examples_by_dependency(session: nox.Session, extras):
    _, extras = extras
    pypi_deps = ",".join(extras["include"])
    pytest_options = " and ".join(extras["include"])
    pytest_options = " and not ".join([pytest_options, *extras.get("exclude", [])])
    pytest_args = ["-k", pytest_options]

    env = {
        "AIRFLOW_HOME": "~/airflow-latest-python-latest",
        "AIRFLOW__CORE__ALLOWED_DESERIALIZATION_CLASSES": "airflow.* astro.*",
    }

    session.install("-e", f".[{pypi_deps}]")
    session.install("-e", ".[tests]")

    # Log all the installed dependencies
    session.log("Installed Dependencies:")
    session.run("pip3", "freeze")

    session.run("airflow", "db", "init", env=env)

    # Since pytest is not installed in the nox session directly, we need to set `external=true`.
    session.run(
        "pytest",
        "tests_integration/test_example_dags.py",
        *pytest_args,
        *session.posargs,
        env=env,
        external=True,
    )


@nox.session()
def lint(session: nox.Session) -> None:
    """Run linters."""
    session.install("pre-commit")
    if session.posargs:
        args = [*session.posargs, "--all-files"]
    else:
        args = ["--all-files", "--show-diff-on-failure"]
    session.run("pre-commit", "run", *args)


@nox.session()
def build(session: nox.Session) -> None:
    """Build release artifacts."""
    session.install("build")

    # TODO: Automate version bumping, Git tagging, and more?

    dist = pathlib.Path("dist")
    if dist.exists() and next(dist.iterdir(), None) is not None:
        session.error(
            "There are files in dist/. Remove them and try again. "
            "You can use `git clean -fxdi -- dist` command to do this."
        )
    dist.mkdir(exist_ok=True)

    session.run("python", "-m", "build", *session.posargs)


@nox.session(python="3.10")
def build_docs(session: nox.Session) -> None:
    """Build release artifacts."""
    session.install("-e", ".[doc]")
    session.chdir("./docs")
    session.run("make", "html")


@nox.session(python=["3.8", "3.9", "3.10", "3.11"])
@nox.parametrize("airflow", ["3.0.1", "3.0.2"])
def generate_constraints(session: nox.Session, airflow) -> None:
    """Generate constraints file"""
    session.install("wheel")
    session.install(f"apache-airflow~={airflow}", ".[all]")
    # Log all the installed dependencies
    session.log("Installed Dependencies:")
    out = session.run("pip3", "list", "--format=freeze", external=True, silent=True)
    pathlib.Path(f"constraints-{session.python}-{airflow}.txt").write_text(out)
    print()
    print(out)


@nox.session()
def release(session: nox.Session) -> None:
    """Publish a release."""
    session.install("twine")
    # TODO: Better artifact checking.
    session.run("twine", "check", *session.posargs)
    session.run("twine", "upload", *session.posargs)
