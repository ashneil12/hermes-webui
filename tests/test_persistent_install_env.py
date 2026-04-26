from pathlib import Path
import subprocess


REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")
INIT_SCRIPT = (REPO / "docker_init.bash").read_text(encoding="utf-8")


def test_dockerfile_sets_persistent_install_prefixes():
    expected = [
        "ENV PYTHONUSERBASE=/home/hermeswebui/.hermes/python",
        "ENV PIPX_HOME=/home/hermeswebui/.hermes/pipx",
        "ENV PIPX_BIN_DIR=/home/hermeswebui/.hermes/bin",
        "ENV UV_TOOL_DIR=/home/hermeswebui/.hermes/uv/tools",
        "ENV UV_TOOL_BIN_DIR=/home/hermeswebui/.hermes/bin",
        "ENV NPM_CONFIG_PREFIX=/home/hermeswebui/.hermes/npm",
        "ENV PNPM_HOME=/home/hermeswebui/.hermes/pnpm",
        "ENV CARGO_HOME=/home/hermeswebui/.hermes/cargo",
        "ENV GOPATH=/home/hermeswebui/.hermes/go",
        "ENV GOBIN=/home/hermeswebui/.hermes/bin",
        "ENV BUN_INSTALL=/home/hermeswebui/.hermes/bun",
        "ENV DENO_INSTALL=/home/hermeswebui/.hermes/deno",
    ]
    for line in expected:
        assert line in DOCKERFILE


def test_dockerfile_path_prefers_persistent_bins():
    path_line = next(line for line in DOCKERFILE.splitlines() if line.startswith("ENV PATH="))
    assert "/home/hermeswebui/.hermes/bin" in path_line
    assert "/home/hermeswebui/.hermes/python/bin" in path_line
    assert "/home/hermeswebui/.hermes/npm/bin" in path_line
    assert "/home/hermeswebui/.hermes/cargo/bin" in path_line
    assert "/home/hermeswebui/.hermes/go/bin" in path_line


def test_init_creates_persistent_install_directories():
    expected = [
        "ensure_persistent_dir PYTHONUSERBASE",
        "ensure_persistent_dir PIPX_HOME",
        "ensure_persistent_dir UV_TOOL_DIR",
        "ensure_persistent_dir NPM_CONFIG_PREFIX",
        "ensure_persistent_dir PNPM_HOME",
        "ensure_persistent_dir CARGO_HOME",
        "ensure_persistent_dir GOPATH",
        "ensure_persistent_dir BUN_INSTALL",
        "ensure_persistent_dir DENO_INSTALL",
    ]
    for needle in expected:
        assert needle in INIT_SCRIPT


def test_init_writes_shell_env_for_interactive_installs():
    assert "persistent-env.sh" in INIT_SCRIPT
    assert "Hermes persistent installer environment" in INIT_SCRIPT
    assert ". \"$HOME/.hermes/persistent-env.sh\"" in INIT_SCRIPT


def test_init_script_syntax_is_valid():
    result = subprocess.run(
        ["bash", "-n", str(REPO / "docker_init.bash")],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
