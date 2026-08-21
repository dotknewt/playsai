setopt local_options null_glob no_case_glob

files=( /home/dotme/Music/**/*.{flac,mp3} )
print -r -- "${#files} files"

for f in $files; do
  fp=$(fpcalc -plain "$f") || continue
  [[ -n $fp ]] || continue
  printf '\n== %s\n' "$f"
  beet chromasearch -c 3 -w -s "$fp" </dev/null
done
