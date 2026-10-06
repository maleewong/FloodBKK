"""Merge river readings fetched on the laptop (RID does not answer the VPS) into data/history.

    python scripts/merge_incoming.py <folder>     folder holding river_YYYY-MM.csv / river_stations.csv
Rows already in data/history are skipped (same station and time). Called by backup_send.sh (river_in).
"""
import csv, glob, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import update as U
import collect as C


def main(src):
    n = 0
    for fn in sorted(glob.glob(os.path.join(src, '**', 'river_*.csv'), recursive=True)):
        base = os.path.basename(fn)
        rows = list(csv.reader(open(fn, encoding='utf-8-sig')))
        if not rows: continue
        if base == 'river_stations.csv' and rows[0] == C.RST_HEAD:
            U.append_csv(U.HIST(base), C.RST_HEAD, rows[1:], key=7)
        elif re.fullmatch(r'river_\d{4}-\d{2}\.csv', base) and rows[0] == C.RIVER_HEAD:
            n += U.append_csv(U.HIST(base), C.RIVER_HEAD, rows[1:])
    print(f'river from laptop: +{n} rows')


if __name__ == '__main__':
    main(sys.argv[1])
