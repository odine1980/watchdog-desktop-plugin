#!/usr/bin/env bash
# Stamp the newest git tag into desktop-plugin/plugin.js — both the @version
# header line and the plugin object's version field. Single source of truth:
# the git tag. Run after `git tag vX.Y.Z` and before committing the release.
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION="$(git describe --tags --abbrev=0 | sed 's/^v//')"
FILE="desktop-plugin/plugin.js"

if ! grep -q '^ \* @version ' "$FILE"; then
  echo "error: no '@version' header line in $FILE" >&2
  echo "add:  * @version X.Y.Z   (right under the first description line)" >&2
  exit 1
fi

sed -i -E "s|^ \* @version .*$| * @version $VERSION|" "$FILE"
sed -i -E "s|^  version: '[^']+',$|  version: '$VERSION',|" "$FILE"

echo "stamped v$VERSION into $FILE"
git diff --stat "$FILE"
