#!/bin/sh
# One gap-wheel build inside its target's image (gap-wheel-build.yml, build-docker): the image's build tools, a pinned
# Rust toolchain (the manylinux and Alpine images have none, and many gap packages need it), then gap_wheels.py build
# with the image's interpreter for the target ($INTERPRETER). Both builds of a pair run in the same image (pinned by
# digest) with the same Rust, so they can come out byte-identical. rustup-init is checked against a pinned sha256.
set -eu

RUSTUP_VERSION=1.29.3
RUST_VERSION=1.100.0

if [ -f /etc/alpine-release ]; then
  apk add --no-cache build-base curl >/dev/null
  libc=musl
else
  libc=gnu
fi
triple="$(uname -m)-unknown-linux-$libc"
case "$triple" in
  x86_64-unknown-linux-gnu) sum=b1c8cb14e30889e7e2999e6b69e62d9e9b9ffc0f4df8180564d306d9680e6c55 ;;
  aarch64-unknown-linux-musl) sum=956deacdc27ab82ae7ac821d5d3e9e95e4795d476d17efe99743bb8531ca1031 ;;
  *) echo "container_build: no pinned rustup-init for $triple" >&2; exit 1 ;;
esac
curl --proto '=https' --tlsv1.2 -sSfL -o /tmp/rustup-init \
  "https://static.rust-lang.org/rustup/archive/$RUSTUP_VERSION/$triple/rustup-init"
echo "$sum  /tmp/rustup-init" | sha256sum -c - >/dev/null
chmod +x /tmp/rustup-init
/tmp/rustup-init -y -q --no-modify-path --profile minimal --default-toolchain "$RUST_VERSION" >/dev/null
rm /tmp/rustup-init
PATH="$HOME/.cargo/bin:$PATH"
export PATH

exec "$INTERPRETER" scripts/gap_wheels.py build --name "$NAME" --version "$VERSION" --target "$TARGET" \
  --attempt "$ATTEMPT" --runner "$RUNNER" --out /out/result.json --wheel-dir /out/wheel
