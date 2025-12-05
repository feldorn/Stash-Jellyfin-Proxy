#!/bin/bash

IMAGE_NAME="stash-jellyfin-proxy"
IMAGE_TAG="latest"
VERSION_TAG="5.00"

show_help() {
    echo "Stash-Jellyfin Proxy Docker Build Script"
    echo ""
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -n, --name NAME    Set image name (default: ${IMAGE_NAME})"
    echo "  -t, --tag TAG      Set image tag (default: ${IMAGE_TAG})"
    echo "  --no-cache         Build without cache"
    echo "  --push             Push to registry after build"
    echo "  -h, --help         Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0                           # Build with defaults"
    echo "  $0 -n myrepo/proxy -t dev    # Custom name and tag"
    echo "  $0 --no-cache                # Fresh build"
}

NO_CACHE=""
PUSH=false

while [[ $# -gt 0 ]]; do
    case $1 in
        -n|--name)
            IMAGE_NAME="$2"
            shift 2
            ;;
        -t|--tag)
            IMAGE_TAG="$2"
            shift 2
            ;;
        --no-cache)
            NO_CACHE="--no-cache"
            shift
            ;;
        --push)
            PUSH=true
            shift
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$SCRIPT_DIR"

if [ ! -f "Dockerfile" ]; then
    echo "Error: Dockerfile not found in ${SCRIPT_DIR}"
    exit 1
fi

if [ ! -f "${PROJECT_DIR}/stash_jellyfin_proxy.py" ]; then
    echo "Error: stash_jellyfin_proxy.py not found in ${PROJECT_DIR}"
    exit 1
fi

if [ ! -f "docker-entrypoint.sh" ]; then
    echo "Error: docker-entrypoint.sh not found in ${SCRIPT_DIR}"
    exit 1
fi

BUILD_DIR=$(mktemp -d)
trap "rm -rf $BUILD_DIR" EXIT

echo "Preparing build context..."
cp "${PROJECT_DIR}/stash_jellyfin_proxy.py" "$BUILD_DIR/"
cp "${SCRIPT_DIR}/Dockerfile" "$BUILD_DIR/"
cp "${SCRIPT_DIR}/docker-entrypoint.sh" "$BUILD_DIR/"

echo "Building ${IMAGE_NAME}:${IMAGE_TAG}..."
echo ""

docker build ${NO_CACHE} \
    -t "${IMAGE_NAME}:${IMAGE_TAG}" \
    -t "${IMAGE_NAME}:${VERSION_TAG}" \
    "$BUILD_DIR"

BUILD_STATUS=$?

if [ $BUILD_STATUS -eq 0 ]; then
    echo ""
    echo "Build successful!"
    echo ""
    echo "Images created:"
    echo "  ${IMAGE_NAME}:${IMAGE_TAG}"
    echo "  ${IMAGE_NAME}:${VERSION_TAG}"
    echo ""
    echo "To run the container:"
    echo "  docker run -d \\"
    echo "    --name stash-jellyfin-proxy \\"
    echo "    -p 8096:8096 \\"
    echo "    -p 8097:8097 \\"
    echo "    -v /path/to/config:/config \\"
    echo "    -e PUID=1000 \\"
    echo "    -e PGID=1000 \\"
    echo "    -e TZ=America/New_York \\"
    echo "    ${IMAGE_NAME}:${IMAGE_TAG}"
    echo ""
    echo "Or use docker-compose:"
    echo "  docker-compose -f ${SCRIPT_DIR}/docker-compose.yml up -d"
    
    if [ "$PUSH" = true ]; then
        echo ""
        echo "Pushing to registry..."
        docker push "${IMAGE_NAME}:${IMAGE_TAG}"
        docker push "${IMAGE_NAME}:${VERSION_TAG}"
    fi
else
    echo ""
    echo "Build failed with status ${BUILD_STATUS}"
    exit $BUILD_STATUS
fi
