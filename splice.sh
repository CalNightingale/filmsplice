#!/bin/sh


# Create list of all clips to splice
ls -1 staging | sed 's/^/file /' > clips.txt

# Put list in staging directory so it is properly wiped on new splice
mv clips.txt staging

# execute concatenation with ffmpeg
ffmpeg -f concat -i staging/clips.txt -c copy staging/__merged.MP4
