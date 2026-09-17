#!/usr/bin/env bash
# AWS Lambda 배포용 zip을 만든다.
#
# Lambda 런타임(Amazon Linux 2023 / x86_64)에서 동작하는 wheel이 필요하므로
# Linux x86_64 머신에서 실행해야 한다. 한글 폰트는 fonts-nanum 패키지에서 가져온다:
#   sudo apt install -y fonts-nanum zip
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${1:-/tmp/traininglog-lambda-build}"
OUT_ZIP="${2:-${HERE}/dist/traininglog-lambda.zip}"

# Lambda 함수에 설정한 런타임과 반드시 일치해야 한다 (바이너리 wheel이 cp<버전> 태그로 고정되기 때문).
PY_VERSION="${PY_VERSION:-3.14}"
FONT_DIR=/usr/share/fonts/truetype/nanum

for font in NanumBarunGothic.ttf NanumBarunGothicBold.ttf; do
  if [[ ! -f "${FONT_DIR}/${font}" ]]; then
    echo "한글 폰트를 찾지 못했습니다: ${FONT_DIR}/${font}" >&2
    echo "  sudo apt install -y fonts-nanum" >&2
    exit 1
  fi
done

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/fonts"

echo "==> 의존성 설치 (Lambda python${PY_VERSION} 호환 wheel만)"
python3 -m pip install \
  --target "$BUILD_DIR" \
  --platform manylinux_2_28_x86_64 \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version "$PY_VERSION" \
  --only-binary=:all: \
  --upgrade \
  -r "${HERE}/requirements.txt"

echo "==> 코드/폰트 복사"
cp "${HERE}/send_daily_report.py" "$BUILD_DIR/"
cp "${FONT_DIR}/NanumBarunGothic.ttf" "${FONT_DIR}/NanumBarunGothicBold.ttf" "$BUILD_DIR/fonts/"

echo "==> zip 생성"
mkdir -p "$(dirname "$OUT_ZIP")"
rm -f "$OUT_ZIP"
(cd "$BUILD_DIR" && zip -qr "$OUT_ZIP" . -x '*.pyc' '*/__pycache__/*')

echo "완료: ${OUT_ZIP} ($(du -h "$OUT_ZIP" | cut -f1))"
