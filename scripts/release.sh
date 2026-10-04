#!/usr/bin/env bash
set -eu

version="${1:-}"
case "$version" in
  *[!0-9A-Za-z.+-]*|'') echo "release: invalid version: $version" >&2; exit 1 ;;
esac
printf '%s\n' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$' || { echo "release: version must look like x.y.z" >&2; exit 1; }

tag="v$version"
branch=$(git branch --show-current)
[ "$branch" = main ] || { echo "release: must run on main" >&2; exit 1; }
[ -z "$(git status --short)" ] || { echo "release: working tree is not clean" >&2; exit 1; }
git rev-parse --verify "$tag^{commit}" >/dev/null 2>&1 && { echo "release: tag already exists: $tag" >&2; exit 1; }
if git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null 2>&1; then
  echo "release: remote tag already exists: $tag" >&2
  exit 1
fi
trap 'git checkout -- VERSION 2>/dev/null' ERR
printf '%s\n' "$version" > VERSION
python3 build.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
bash tests/test_shell.sh
python3 build.py --check

git add VERSION
git commit -m "Release $tag

Co-Authored-By: Claude Code <noreply@anthropic.com>"
git tag -a "$tag" -m "Release $tag"
git push --atomic origin main "$tag"
printf 'released %s; GitHub Actions will publish the assets\n' "$tag"
