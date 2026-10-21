#!/bin/sh
# Public binary installer. Keep the entry point last so a partial script cannot install.

main() (
    set -eu
    umask 077
    export LC_ALL=C
    unset TAR_OPTIONS TAPE GZIP GZIP_OPT

    fail() { printf 'co installer: %s\n' "$*" >&2; exit 1; }
    [ "$#" -eq 0 ] || fail "No arguments accepted; set CO_INSTALL_DIR to an absolute directory."
    for tool in awk chmod curl mkdir mktemp mv rm rmdir sed tar tr uname wc; do
        command -v "$tool" >/dev/null 2>&1 || fail "Required utility not found: $tool"
    done
    if command -v sha256sum >/dev/null 2>&1; then
        checksum_tool=sha256sum
    elif command -v shasum >/dev/null 2>&1; then
        checksum_tool=shasum
    else
        fail "SHA256 verification requires sha256sum or shasum."
    fi

    case "$(uname -s)" in
        Darwin)
            os=darwin
            command -v sw_vers >/dev/null 2>&1 || fail "Cannot determine macOS version."
            mac_version=$(sw_vers -productVersion) || fail "Cannot determine macOS version."
            mac_major=${mac_version%%.*}
            case "$mac_major" in ''|*[!0-9]*) fail "Invalid macOS version." ;; esac
            [ "$mac_major" -ge 13 ] || fail "co requires macOS 13 (Ventura) or newer."
            ;;
        Linux) os=linux ;;
        *) fail "Unsupported operating system; supported: macOS and Linux." ;;
    esac
    case "$(uname -m)" in
        arm64|aarch64) arch=arm64 ;;
        x86_64|amd64) arch=amd64 ;;
        *) fail "Unsupported architecture; supported: arm64 and x86_64." ;;
    esac
    install_dir=${CO_INSTALL_DIR-${HOME:?HOME must be set}/.local/bin}
    case "$install_dir" in /*) ;; *) fail "CO_INSTALL_DIR must be an absolute directory." ;; esac
    target=$install_dir/co
    [ ! -L "$target" ] && [ ! -d "$target" ] ||
        fail "Refusing to replace a symlink or directory at $target; use its package manager."

    work=$(mktemp -d "${TMPDIR:-/tmp}/co-download.XXXXXX") || fail "Cannot create download directory."
    stage=
    cleanup() {
        result=$?
        trap - 0 HUP INT TERM
        if [ -n "$stage" ]; then
            rm -f "$stage/co" && rmdir "$stage" ||
                printf 'co installer: could not clean staging directory %s\n' "$stage" >&2
        fi
        rm -f "$work/index" "$work/selected" "$work/token.json" "$work/token-line" \
            "$work/archive.tar.gz" "$work/names" "$work/details" ||
            printf 'co installer: could not clean downloaded files in %s\n' "$work" >&2
        rmdir "$work" || printf 'co installer: could not remove %s\n' "$work" >&2
        exit "$result"
    }
    trap cleanup 0
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    download() {
        url=$1 destination=$2 limit=$3
        shift 3
        curl -q --fail --silent --show-error --location \
            --proto '=https' --proto-redir '=https' --tlsv1.2 \
            --connect-timeout 15 --max-time 300 --retry 2 --max-filesize "$limit" \
            "$@" --output "$destination" "$url" || fail "Download failed: $url"
        size=$(wc -c < "$destination")
        [ "$size" -gt 0 ] && [ "$size" -le "$limit" ] || fail "Invalid download size: $url"
    }

    download "https://raw.githubusercontent.com/colbylwilliams/homebrew-tap/main/releases/latest.txt" \
        "$work/index" 4096
    # Validate the entire index, not only the selected platform. Never eval remote data.
    if ! awk -v os="$os" -v arch="$arch" '
        function bad() { failed = 1; exit 1 }
        NR == 1 { if ($0 != "co-release-v1") bad(); next }
        NR == 2 {
            if (NF != 2 || $1 != "version" ||
                $2 !~ /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/) bad()
            version = $2; next
        }
        NR == 3 {
            if (NF != 2 || $1 != "manifest" || length($2) != 71 ||
                $2 !~ /^sha256:[0-9a-f]+$/) bad()
            next
        }
        NR == 4 {
            if (NF != 2 || $1 != "revision" || length($2) != 40 || $2 !~ /^[0-9a-f]+$/) bad()
            next
        }
        NR > 4 {
            key = $1 " " $2
            if (NF != 4 || $1 !~ /^(darwin|linux)$/ || $2 !~ /^(amd64|arm64)$/ ||
                length($3) != 64 || $3 !~ /^[0-9a-f]+$/ || $4 !~ /^[1-9][0-9]*$/ ||
                $4 > 134217728 || seen[key]++) bad()
            if ($1 == os && $2 == arch) selected = version " " $3 " " $4
        }
        END { if (failed || NR != 8 || selected == "") exit 1; print selected }
    ' "$work/index" > "$work/selected"; then
        fail "Malformed release index; no files have been installed."
    fi
    read -r version checksum expected_size < "$work/selected"
    download "https://ghcr.io/token?service=ghcr.io&scope=repository:colbylwilliams/co/cli:pull" \
        "$work/token.json" 65536
    tr -d '\r\n' < "$work/token.json" > "$work/token-line"
    # GHCR anonymous token responses are a single token object. Reject unknown shapes.
    token=$(sed -n 's|^[[:space:]]*{[[:space:]]*"token"[[:space:]]*:[[:space:]]*"\([A-Za-z0-9._~+/=-][A-Za-z0-9._~+/=-]*\)"[[:space:]]*}[[:space:]]*$|\1|p' "$work/token-line")
    [ -n "$token" ] || fail "Invalid GHCR anonymous token response."
    download "https://ghcr.io/v2/colbylwilliams/co/cli/blobs/sha256:$checksum" \
        "$work/archive.tar.gz" "$expected_size" --header "Authorization: Bearer $token"
    [ "$(wc -c < "$work/archive.tar.gz")" -eq "$expected_size" ] || fail "Archive size mismatch."
    if [ "$checksum_tool" = sha256sum ]; then
        actual=$(sha256sum "$work/archive.tar.gz") || fail "SHA256 verification failed."
    else
        actual=$(shasum -a 256 "$work/archive.tar.gz") || fail "SHA256 verification failed."
    fi
    actual=${actual%% *}
    [ "$actual" = "$checksum" ] || fail "Archive checksum mismatch."
    tar -tzf "$work/archive.tar.gz" > "$work/names" || fail "Cannot list release archive."
    awk '($0 != "co" && $0 != "LICENSE") || seen[$0]++ { exit 1 }
         END { if (NR != 2 || !seen["co"] || !seen["LICENSE"]) exit 1 }' "$work/names" ||
        fail "Archive must contain exactly root co and LICENSE."
    tar -tvzf "$work/archive.tar.gz" > "$work/details" || fail "Cannot inspect release archive."
    awk 'substr($0, 1, 1) != "-" { exit 1 } END { if (NR != 2) exit 1 }' "$work/details" ||
        fail "Archive entries must be regular files."

    mkdir -p "$install_dir" || fail "Cannot create $install_dir."
    stage=$(mktemp -d "$install_dir/.co-install.XXXXXX") || fail "Cannot stage an install in $install_dir."
    # POSIX ulimit uses 512-byte blocks; bash uses 1024 outside POSIX mode.
    # This installer runs as sh, so cap the streamed output at 256 MiB.
    if ! (ulimit -f 524288 && tar -xOzf "$work/archive.tar.gz" co > "$stage/co"); then
        fail "Cannot extract co (file-size limit exceeded or archive invalid)."
    fi
    [ -s "$stage/co" ] || fail "The release executable is empty."
    [ "$(wc -c < "$stage/co")" -le 268435456 ] || fail "The release executable exceeds 256 MiB."
    chmod 755 "$stage/co" || fail "Cannot make staged co executable."
    [ ! -L "$target" ] && [ ! -d "$target" ] || fail "Install target changed; refusing replacement."
    mv -f "$stage/co" "$target" || fail "Cannot replace $target; previous installation preserved."
    printf 'Installed co %s to %s\n' "$version" "$target"
    case ":${PATH:-}:" in
        *":$install_dir:"*) ;;
        *) printf 'Add %s to your PATH to run co.\n' "$install_dir" ;;
    esac
)

main "$@"
