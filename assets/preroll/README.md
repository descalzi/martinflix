# Plex pre-roll videos

Short clips Plex plays before a film. Setup mounts this folder read-only into Plex at `/preroll` and sets Plex's
pre-roll list to every `.mp4` here, played at random, one per film.

To change the clips, add or remove `.mp4` files here and rerun setup with `--update`. Keep each file well under
GitHub's 100 MB limit.
