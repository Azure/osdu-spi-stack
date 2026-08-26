# Installation

SPI Stack is distributed as a versioned Python wheel attached to each
[GitHub Release](https://github.com/Azure/osdu-spi-stack/releases). The
[`uv`](https://docs.astral.sh/uv/) tool installs and manages the `spi` executable.

## Latest release

The latest-release commands resolve the newest wheel from GitHub, so they do not
contain a version number.

**macOS and Linux**

```bash
uv tool install "$(curl -fsSL https://api.github.com/repos/Azure/osdu-spi-stack/releases/latest \
  | grep -o 'https://github.com/Azure/osdu-spi-stack/releases/download/[^"]*-py3-none-any.whl')"
```

**Windows PowerShell**

```powershell
uv tool install (irm https://api.github.com/repos/Azure/osdu-spi-stack/releases/latest).assets.where({ $_.name -like '*-py3-none-any.whl' }).browser_download_url
```

Verify the installed version:

```bash
spi --version
```

The installed `spi` executable is on `PATH`. Commands in this guide do not require
the `uv run` prefix.

## Pinned release

Install a specific wheel for CI, reproducible environments, or bug reports:

```bash
uv tool install https://github.com/Azure/osdu-spi-stack/releases/download/v0.1.0/spi-0.1.0-py3-none-any.whl
```

Copy the wheel URL for another version from its
[release page](https://github.com/Azure/osdu-spi-stack/releases).

## Upgrade

The installed CLI resolves newer wheels from GitHub Releases:

```bash
spi update           # Check for and install a newer version
spi update --check   # Report whether an update is available
spi update --force   # Reinstall the latest version
```

On native Windows installations managed by `uv`, `spi update` exits before
replacing its active tool environment. Run the recovery command it prints from a
new terminal:

```powershell
uv tool install --force <wheel-url>
```

## Git installation

`uv` can install directly from a Git tag:

```bash
uv tool install git+https://github.com/Azure/osdu-spi-stack.git@v0.1.0
```

Release wheels are preferred because they preserve the tag-derived value reported
by `spi --version`.
