#!/bin/sh
# Install the MNN Press on a Raspberry Pi, or any Debian-family machine:
#
#   curl -fsSL https://raw.githubusercontent.com/JoshuaHayesWasHere/MNN/main/install.sh | sh
#
# It installs Docker with the compose plugin if either is missing, puts the
# Press in ~/mnn (or updates the copy already there), writes .env if there is
# none, starts the Press, and prints the line to run on the Kindle. It never
# prompts, and running it again updates and restarts the Press.
#
# Options go after `sh -s --`, or in the environment; see usage below.

set -eu

# The line the reader runs on the Kindle next. The Press serves that script;
# if its address changes, this is the one place to change it.
kindle_install_line() {
    printf 'curl http://%s:%s/kindle/install.sh | sh\n' "$1" "$2"
}

# Where the Press is installed. Fixed, so a second run finds the first. Compose
# names its project after the directory.
PRESS_PROJECT=mnn
PRESS_DIR=${HOME:?HOME is not set}/$PRESS_PROJECT

# What a first install tracks unless --repo or --ref says otherwise.
DEFAULT_REPO=https://github.com/JoshuaHayesWasHere/MNN.git
DEFAULT_REF=main

# Empty means "not asked for": a first install then tracks the defaults above,
# the port is compose's own default, and the time zone this machine's. The port
# and the time zone are taken out of the environment, where compose would read
# them ahead of .env.
REPO_OPTION=${MNN_REPO:-}
REF_OPTION=${MNN_REF:-}
PORT_OPTION=${PRESS_PORT:-}
TZ_OPTION=${TZ:-}
unset PRESS_PORT TZ

DOCKER_AS_ROOT=0
APT_UPDATED=0

usage() {
    cat <<'EOF'
Install or update the MNN Press in ~/mnn and start it with docker compose.

  curl -fsSL https://raw.githubusercontent.com/JoshuaHayesWasHere/MNN/main/install.sh | sh
  curl -fsSL https://raw.githubusercontent.com/JoshuaHayesWasHere/MNN/main/install.sh | sh -s -- --port 9000

Options (flag, or environment variable):
  --port N    PRESS_PORT  Port the Kindle reaches the Press on (default 8484).
                          Written to .env only when .env does not exist yet.
  --repo URL  MNN_REPO    Git repository the first install tracks.
  --ref NAME  MNN_REF     Branch the first install tracks (default main).
  -h, --help              Show this.

A later run updates from the repository and branch the install already tracks,
and stops if --repo or --ref names another.

TZ, if set, is used as the Press's time zone instead of this machine's. Like
the port, it is written to .env only when .env does not exist yet.
EOF
}

say() { printf '%s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Prints a directory's path with symlinks resolved. If the directory is gone,
# its parent is resolved instead; if that is gone too, the path is as given.
real_dir() {
    (cd -P "$1" 2>/dev/null && pwd -P) && return 0
    parent=$(cd -P "$(dirname "$1")" 2>/dev/null && pwd -P) || {
        printf '%s\n' "$1"
        return 0
    }
    printf '%s/%s\n' "${parent%/}" "$(basename "$1")"
}

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif have sudo; then
        sudo "$@"
    else
        die "this step needs root and sudo is not installed. Run the installer as root."
    fi
}

# Says why sudo is about to be used. Silent for root, who needs none.
sudo_note() {
    [ "$(id -u)" -eq 0 ] || say "  (using sudo: $1)"
}

dkr() {
    if [ "$DOCKER_AS_ROOT" -eq 1 ]; then
        as_root docker "$@"
    else
        docker "$@"
    fi
}

compose() {
    (cd "$PRESS_DIR" && dkr compose "$@")
}

apt_update() {
    [ "$APT_UPDATED" -eq 0 ] || return 0
    as_root apt-get update -q
    APT_UPDATED=1
}

apt_install() {
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$@"
}

# Make sure a command exists, installing the apt package of the same name.
need_tool() {
    have "$1" && return 0
    have apt-get || die "$1 is needed and this system has no apt-get to install it with. Install $1 and run this again."
    say "Installing $1."
    sudo_note "installing packages needs root"
    apt_update
    apt_install ca-certificates "$1"
}

check_system() {
    [ "$(uname -s)" = Linux ] || die "the Press runs on Linux; this is $(uname -s)."
    arch=$(dpkg --print-architecture 2>/dev/null || uname -m)
    case "$arch" in
        amd64 | x86_64 | arm64 | aarch64) ;;
        *) die "the Press image builds for arm64 and amd64 only, and this system is $arch. On a Raspberry Pi, use the 64-bit Raspberry Pi OS." ;;
    esac
}

# Sets DOCKER_DISTRO and DOCKER_CODENAME to this system's place in Docker's
# apt repository. Raspberry Pi OS (64-bit) is Debian there.
docker_apt_target() {
    unsupported="this installer can install Docker only on Debian, Ubuntu, Raspberry Pi OS and their derivatives. Install Docker Engine with the compose plugin (https://docs.docker.com/engine/install/) and run this again."
    have apt-get || die "Docker with the compose plugin is missing and this system has no apt-get: $unsupported"
    [ -r /etc/os-release ] || die "Docker with the compose plugin is missing and /etc/os-release cannot be read: $unsupported"
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}" in
        debian)
            DOCKER_DISTRO=debian
            DOCKER_CODENAME=${VERSION_CODENAME:-}
            ;;
        ubuntu)
            DOCKER_DISTRO=ubuntu
            DOCKER_CODENAME=${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}
            ;;
        *)
            case " ${ID_LIKE:-} " in
                *" ubuntu "*)
                    DOCKER_DISTRO=ubuntu
                    DOCKER_CODENAME=${UBUNTU_CODENAME:-}
                    ;;
                *" debian "*)
                    DOCKER_DISTRO=debian
                    DOCKER_CODENAME=${DEBIAN_CODENAME:-${VERSION_CODENAME:-}}
                    ;;
                *) die "Docker with the compose plugin is missing and this system (${ID:-unknown}) is not Debian-based: $unsupported" ;;
            esac
            ;;
    esac
    [ -n "$DOCKER_CODENAME" ] || die "Docker with the compose plugin is missing and this system's release name is unknown: $unsupported"
}

# Docker's documented apt-repository install:
# https://docs.docker.com/engine/install/debian/ (the same steps serve Ubuntu
# and 64-bit Raspberry Pi OS). Arguments are the packages to install.
install_from_docker_repo() {
    need_tool curl
    url=https://download.docker.com/linux/$DOCKER_DISTRO
    curl -fsSI "$url/dists/$DOCKER_CODENAME/Release" >/dev/null 2>&1 ||
        die "Docker has no apt repository for $DOCKER_DISTRO $DOCKER_CODENAME (or download.docker.com cannot be reached). Install Docker Engine with the compose plugin yourself (https://docs.docker.com/engine/install/) and run this again."
    if ! grep -rqs download.docker.com /etc/apt/sources.list /etc/apt/sources.list.d; then
        as_root install -m 0755 -d /etc/apt/keyrings
        as_root curl -fsSL "$url/gpg" -o /etc/apt/keyrings/docker.asc
        as_root chmod a+r /etc/apt/keyrings/docker.asc
        printf '%s\n' \
            "Types: deb" \
            "URIs: $url" \
            "Suites: $DOCKER_CODENAME" \
            "Components: stable" \
            "Signed-By: /etc/apt/keyrings/docker.asc" |
            as_root tee /etc/apt/sources.list.d/docker.sources >/dev/null
    fi
    APT_UPDATED=0
    apt_update
    apt_install "$@"
}

ensure_docker() {
    if ! have docker; then
        docker_apt_target
        say "Docker is not installed. Installing it from Docker's apt repository."
        sudo_note "adding a repository and installing packages needs root"
        install_from_docker_repo docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        if [ "$(id -u)" -ne 0 ]; then
            user=$(id -un)
            as_root usermod -aG docker "$user"
            say "Added $user to the docker group, so docker works without sudo from your next login."
        fi
    fi

    # Reach the daemon: as this user if possible, through sudo if this session
    # is not in the docker group, and starting the service if it is down.
    if ! docker info >/dev/null 2>&1; then
        if [ "$(id -u)" -ne 0 ]; then
            have sudo || die "Docker is installed but does not answer this user, and there is no sudo to reach it as root. Run the installer as root, or as a user in the docker group with the Docker service running."
            sudo_note "this session cannot reach Docker without it"
            sudo docker --version >/dev/null ||
                die "sudo did not run docker as root for this user (its message is above). Run the installer as root, or as a user who may use sudo."
            DOCKER_AS_ROOT=1
        fi
        if ! dkr info >/dev/null 2>&1; then
            if have systemctl; then
                say "Starting the Docker service."
                sudo_note "managing services needs root"
                as_root systemctl enable --now docker || true
            fi
            tries=0
            until dkr info >/dev/null 2>&1; do
                tries=$((tries + 1))
                [ "$tries" -lt 15 ] || die "Docker is installed but its daemon is not answering. Start it (sudo systemctl start docker) and run this again."
                sleep 2
            done
            if docker info >/dev/null 2>&1; then
                DOCKER_AS_ROOT=0
            fi
        fi
        [ "$DOCKER_AS_ROOT" -eq 0 ] || say "This session is not in the docker group, so Docker is run through sudo."
    fi

    if ! dkr compose version >/dev/null 2>&1; then
        docker_apt_target
        say "Docker's compose plugin is missing. Installing it from Docker's apt repository."
        sudo_note "adding a repository and installing packages needs root"
        install_from_docker_repo docker-compose-plugin docker-buildx-plugin
        dkr compose version >/dev/null 2>&1 || die "docker compose still does not work after installing the plugin."
    fi
}

# Compose finds a project's containers by its name, not its directory, so
# starting this copy would replace the containers of any other copy of the
# Press that runs under the same name, and with them its settings.
check_project() {
    here=$(real_dir "$PRESS_DIR")
    dirs=$(dkr ps -a --filter "label=com.docker.compose.project=$PRESS_PROJECT" --format '{{.Label "com.docker.compose.project.working_dir"}}')
    while IFS= read -r dir; do
        [ -n "$dir" ] || continue
        [ "$(real_dir "$dir")" != "$here" ] || continue
        die "compose project '$PRESS_PROJECT' already has containers started from $dir, and installing into $PRESS_DIR would replace them with ones that lack that copy's settings. Update that copy where it is, or remove its containers (docker compose -p $PRESS_PROJECT down) and run this again."
    done <<EOF
$dirs
EOF
}

# Arguments are the branch and the repository.
cannot_fetch() {
    die "could not fetch $1 from $2 (git's message is above). Check the address and the network. The installer never asks for a login, so a private repository needs a git credential helper set up first."
}

# Whether two repository addresses name the same place. Git records a local
# path as an absolute one, so directories are compared by where they lead.
same_repo() {
    [ "$1" != "$2" ] || return 0
    [ -d "$1" ] && [ -d "$2" ] && [ "$(real_dir "$1")" = "$(real_dir "$2")" ]
}

fetch_press() {
    need_tool git
    export GIT_TERMINAL_PROMPT=0
    if [ -d "$PRESS_DIR/.git" ]; then
        repo=$(git -C "$PRESS_DIR" config --get remote.origin.url) ||
            die "$PRESS_DIR has no origin repository to update from. Move it away and run this again."
        ref=$(git -C "$PRESS_DIR" symbolic-ref --quiet --short HEAD) ||
            die "$PRESS_DIR is not on a branch, so there is nothing for an update to follow. Put it back on one (git -C $PRESS_DIR switch <branch>) and run this again."
        if ! same_repo "${REPO_OPTION:-$repo}" "$repo" || [ "${REF_OPTION:-$ref}" != "$ref" ]; then
            die "$PRESS_DIR tracks $ref from $repo, and this run asked for ${REF_OPTION:-$ref} from ${REPO_OPTION:-$repo}. --repo and --ref choose what a first install tracks: leave them out to update this copy, or move $PRESS_DIR away to install the other."
        fi
        say "Updating the Press in $PRESS_DIR."
        git -C "$PRESS_DIR" fetch --quiet origin "$ref" || cannot_fetch "$ref" "$repo"
        git -C "$PRESS_DIR" merge --quiet --ff-only FETCH_HEAD ||
            die "could not update $PRESS_DIR (git's message is above). Edits to the Press's own files block an update: settings belong in .env."
    elif [ -e "$PRESS_DIR" ]; then
        die "$PRESS_DIR exists but is not a copy of the Press. Move it away and run this again."
    else
        repo=${REPO_OPTION:-$DEFAULT_REPO}
        ref=${REF_OPTION:-$DEFAULT_REF}
        say "Fetching the Press into $PRESS_DIR."
        found=0
        git ls-remote --exit-code "$repo" "refs/heads/$ref" >/dev/null || found=$?
        case "$found" in
            0) ;;
            2) die "$ref is not a branch of $repo. --ref takes a branch, not a tag or a commit, because later runs update the install along it." ;;
            *) cannot_fetch "$ref" "$repo" ;;
        esac
        git clone --quiet --branch "$ref" "$repo" "$PRESS_DIR" || cannot_fetch "$ref" "$repo"
    fi
}

valid_timezone() {
    case "$1" in
        '' | *[!A-Za-z0-9_+/-]*) return 1 ;;
    esac
}

host_timezone() {
    tz=$TZ_OPTION
    if ! valid_timezone "$tz" && [ -L /etc/localtime ]; then
        tz=$(readlink /etc/localtime | sed -n 's|^.*/zoneinfo/||p')
    fi
    if ! valid_timezone "$tz" && have timedatectl; then
        tz=$(timedatectl show -p Timezone --value 2>/dev/null || true)
    fi
    if ! valid_timezone "$tz" && [ -r /etc/timezone ]; then
        tz=$(head -n 1 /etc/timezone)
    fi
    valid_timezone "$tz" || tz=UTC
    printf '%s\n' "$tz"
}

write_env() {
    env_file=$PRESS_DIR/.env
    if [ -e "$env_file" ]; then
        say "Keeping the existing $env_file."
        [ -z "$PORT_OPTION" ] || say "The port option is ignored because .env already exists: set PRESS_PORT there."
        ! valid_timezone "$TZ_OPTION" || say "TZ is ignored because .env already exists: set TZ there."
        return 0
    fi
    tz=$(host_timezone)
    (
        # It will hold the staging token one day, so only its owner reads it.
        umask 077
        {
            say "# Written by install.sh, which never overwrites it."
            say "# .env.example lists every setting."
            say ""
            say "# This machine's time zone, so EDITION_TIME means local time."
            say "TZ=$tz"
            if [ -n "$PORT_OPTION" ]; then
                say ""
                say "# Port the Kindle reaches the Press on."
                say "PRESS_PORT=$PORT_OPTION"
            fi
        } >"$env_file"
    )
    say "Wrote $env_file (time zone $tz)."
}

start_press() {
    say "Building and starting the Press. The first build takes a few minutes on a Pi."
    compose up -d --build --force-recreate

    say "Waiting for the Press to answer."
    tries=0
    while :; do
        cid=$(compose ps -q press-server 2>/dev/null || true)
        state=""
        if [ -n "$cid" ]; then
            state=$(dkr inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || true)
        fi
        case "$state" in
            healthy | running) return 0 ;;
        esac
        tries=$((tries + 1))
        [ "$tries" -lt 90 ] || die "the Press did not become healthy within three minutes. In $PRESS_DIR, see: docker compose logs press-server"
        sleep 2
    done
}

published_port() {
    port=$(compose port press-server 8484 2>/dev/null | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p' | head -n 1)
    printf '%s\n' "${port:-8484}"
}

lan_address() {
    addr=""
    if have ip; then
        addr=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1)
    fi
    if [ -z "$addr" ] && have hostname; then
        addr=$(hostname -I 2>/dev/null | awk '{print $1}')
    fi
    printf '%s\n' "${addr:-<press-address>}"
}

valid_port() {
    case "$1" in
        '' | *[!0-9]* | ??????*) return 1 ;;
    esac
    [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --port | --repo | --ref)
                [ $# -ge 2 ] || die "$1 needs a value."
                case "$1" in
                    --port) PORT_OPTION=$2 ;;
                    --repo) REPO_OPTION=$2 ;;
                    --ref) REF_OPTION=$2 ;;
                esac
                shift 2
                ;;
            --port=*) PORT_OPTION=${1#*=}; shift ;;
            --repo=*) REPO_OPTION=${1#*=}; shift ;;
            --ref=*) REF_OPTION=${1#*=}; shift ;;
            -h | --help) usage; exit 0 ;;
            *) die "unknown option: $1 (see --help)" ;;
        esac
    done
    [ -z "$PORT_OPTION" ] || valid_port "$PORT_OPTION" ||
        die "the port must be a number from 1 to 65535, not '$PORT_OPTION'."

    check_system
    ensure_docker
    check_project
    fetch_press
    write_env
    start_press

    address=$(lan_address)
    port=$(published_port)
    say ""
    say "The Press is running at http://$address:$port (installed in $PRESS_DIR)."
    say ""
    say "Next, the Kindle. With nothing to type: on a computer on this network,"
    say "open this address, and save the file into the Kindle's documents folder"
    say "over USB. Then, in KOReader's file browser, long-press it and choose Execute:"
    say ""
    say "  http://$address:$port/kindle/install.sh?download"
    say ""
    say "Or type one line in KOReader's terminal:"
    say ""
    say "  $(kindle_install_line "$address" "$port")"
    say ""
}

# Called on the last line so a download cut short runs nothing, and with no
# stdin so nothing here can read the rest of a piped script, or wait for input.
main "$@" </dev/null
