# Mech Royale bot: team Beams & Bytes

Our entry for the **Mech Royale** track of the Builders Buildathon (Ohio State, Nov 20–22, 2026).

A bot that pilots a mech in a 16-mech battle royale on the OSU campus. It speaks the `mechfight/1` JSON-lines protocol on stdin/stdout and uses only the Python standard library.

## Strategy

- **Survive first.** Predicts the shrinking zone and moves in early. Retreats to regenerate when losing a fight.
- **Map-aware.** At startup it rasterizes `/arena/v2/world.json` and the terrain into a walk grid (4 m, A* path-finding around buildings) and a solid grid (2 m, line-of-fire checks).
- **Loot early.** Weapons, power cubes, crates, NPC drones and supply drops (full HP + 3 cubes).
- **Fight well.** Faces the enemy (rear armour takes ×1.5), strafes to dodge, sidesteps incoming shells and bullets, and leads moving targets. It only fires when the line of fire is clear.
- **All four weapons.** Gun, laser (charge and release), mortar (ballistic solution over buildings) and lightning.
- **Upgrades.** Picked in priority order: armour and HP first, then fire rate and damage.

## Run

```sh
docker build -t beams-and-bytes .
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m \
  --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" beams-and-bytes
```

Team: Venuja Hewavitharana (UTS).
