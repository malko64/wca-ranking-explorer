#!/usr/bin/env python3
"""
Resynchronise les données statistiques de WCA Ranking Explorer depuis l'export
officiel de la base WCA (worldcubeassociation.org/export/results).

Pourquoi ce script existe : la WCA n'expose, dans son API publique, aucun
endpoint donnant en direct le nombre de participants par épreuve, qui détient
le record du monde actuel (et où), les podiums/records nationaux par pays, le
nombre de titres/podiums de championnat d'un cubeur, ou si une compétition
donnée est un championnat national. La seule source pour ces informations est
l'export complet de la base WCA (TSV, ~650 Mo décompressé). Ce script télécharge
cet export, recalcule les données correspondantes, et met à jour :
  - index.html (constantes EVENT_TOTALS*, EVENT_RECORDS*, CHAMPIONSHIP_COMPS,
    COUNTRY_STATS_DATE, CAREER_STATS_DATE)
  - country_stats.json

Lancé automatiquement une fois par jour par .github/workflows/sync-wca-data.yml.
Peut aussi être lancé à la main : `python3 scripts/sync_wca_data.py`
(optionnellement avec `--tsv-dir <dossier>` pour réutiliser un export déjà
téléchargé/décompressé, utile en développement pour ne pas re-télécharger
~380 Mo à chaque essai).
"""
import argparse
import csv
import json
import os
import re
import sys
import tempfile
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime, timezone

EXPORT_URL = 'https://www.worldcubeassociation.org/export/results/v2/tsv'

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML_PATH = os.path.join(REPO_ROOT, 'index.html')
COUNTRY_STATS_PATH = os.path.join(REPO_ROOT, 'country_stats.json')

FR_MONTHS = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet',
             'août', 'septembre', 'octobre', 'novembre', 'décembre']

NEEDED_FILES = [
    'results.tsv', 'ranks_single.tsv', 'ranks_average.tsv', 'persons.tsv',
    'countries.tsv', 'championships.tsv', 'round_types.tsv', 'formats.tsv',
    'competitions.tsv',
]


def log(*args):
    print(*args, flush=True)


def fr_date(d):
    return f"{d.day} {FR_MONTHS[d.month - 1]} {d.year}"


def download_export(dest_dir):
    """Télécharge et décompresse l'export WCA dans dest_dir. Ne garde que les
    fichiers TSV dont ce script a besoin (le reste -- scrambles, result_attempts --
    pèse des centaines de Mo de plus pour rien ici)."""
    zip_path = os.path.join(dest_dir, 'wca_export.zip')
    log(f"Téléchargement de l'export WCA depuis {EXPORT_URL} ...")
    urllib.request.urlretrieve(EXPORT_URL, zip_path)
    log('Téléchargement terminé, décompression des fichiers nécessaires...')
    with zipfile.ZipFile(zip_path) as z:
        for name in NEEDED_FILES:
            member = f'WCA_export_{name}'
            with z.open(member) as src, open(os.path.join(dest_dir, name), 'wb') as dst:
                dst.write(src.read())
    os.remove(zip_path)
    log('Export prêt dans', dest_dir)


def load_tsv(path):
    with open(path, encoding='utf-8') as f:
        return list(csv.DictReader(f, delimiter='\t'))


def valid_time(s):
    try:
        v = int(s)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# EVENT_TOTALS / EVENT_COUNTRY_TOTALS / EVENT_CONTINENT_TOTALS
#
# Nombre de cubeurs distincts possédant au moins un résultat officiel pour
# chaque épreuve (= une ligne dans ranks_single.tsv pour cette épreuve), et sa
# ventilation par pays / continent ACTUEL de chaque personne (persons.tsv,
# sub_id=1 = fiche courante, cf. Person.current côté WCA).
# ---------------------------------------------------------------------------
def compute_event_totals(tsv_dir, countries_by_name):
    log('Calcul de EVENT_TOTALS / EVENT_COUNTRY_TOTALS / EVENT_CONTINENT_TOTALS...')
    person_country = {}
    for row in load_tsv(os.path.join(tsv_dir, 'persons.tsv')):
        if row['sub_id'] == '1':
            person_country[row['wca_id']] = row['country_id']

    totals = defaultdict(int)
    country_totals = defaultdict(lambda: defaultdict(int))
    continent_totals = defaultdict(lambda: defaultdict(int))

    with open(os.path.join(tsv_dir, 'ranks_single.tsv'), encoding='utf-8') as f:
        r = csv.DictReader(f, delimiter='\t')
        for row in r:
            eid = row['event_id']
            totals[eid] += 1
            cname = person_country.get(row['person_id'])
            if not cname:
                continue
            country = countries_by_name.get(cname)
            if not country:
                continue
            country_totals[eid][country['iso2']] += 1
            continent_totals[eid][country['continent_id']] += 1

    return dict(totals), {k: dict(v) for k, v in country_totals.items()}, {k: dict(v) for k, v in continent_totals.items()}


# ---------------------------------------------------------------------------
# EVENT_RECORDS : recordman(s) du monde single/average par épreuve, avec la
# première compétition où le drapeau WR a été posé pour la valeur actuelle.
# ---------------------------------------------------------------------------
def compute_event_records(tsv_dir, comp_dates):
    log('Calcul de EVENT_RECORDS...')

    def world_number_one(filename, value_field):
        holders = defaultdict(list)  # event_id -> [(person_id, value)]
        with open(os.path.join(tsv_dir, filename), encoding='utf-8') as f:
            r = csv.DictReader(f, delimiter='\t')
            for row in r:
                if row['world_rank'] == '1':
                    holders[row['event_id']].append((row['person_id'], int(row[value_field])))
        return holders

    single_holders = world_number_one('ranks_single.tsv', 'best')
    average_holders = world_number_one('ranks_average.tsv', 'best')

    # candidates[(event_id, 'single'|'average')] = {(person_id, value): None}
    candidates = {}
    for eid, lst in single_holders.items():
        for pid, val in lst:
            candidates[(eid, 'single', pid)] = val
    for eid, lst in average_holders.items():
        for pid, val in lst:
            candidates[(eid, 'average', pid)] = val

    # Pour chaque candidat, on cherche en un seul passage sur results.tsv la
    # première compétition (par date) où regional_*_record == 'WR' ET la
    # valeur de ce résultat correspond exactement au record actuel. On garde
    # aussi, en secours, la toute première occurrence de la valeur (sans
    # exiger le flag WR) pour les rares cas où le flag manquerait en données.
    best_match = {}       # key -> (date_tuple, competition_id), avec flag WR
    fallback_match = {}   # key -> (date_tuple, competition_id), sans exiger WR

    with open(os.path.join(tsv_dir, 'results.tsv'), encoding='utf-8') as f:
        r = csv.reader(f, delimiter='\t')
        header = next(r)
        idx = {h: i for i, h in enumerate(header)}
        count = 0
        for row in r:
            count += 1
            if count % 1000000 == 0:
                log(f'  results.tsv (EVENT_RECORDS): {count} lignes...')
            pid = row[idx['person_id']]
            eid = row[idx['event_id']]
            comp = row[idx['competition_id']]
            best = valid_time(row[idx['best']])
            avg = valid_time(row[idx['average']])
            ssrec = row[idx['regional_single_record']]
            sarec = row[idx['regional_average_record']]
            d = comp_dates.get(comp, (9999, 12, 31))

            key_s = (eid, 'single', pid)
            if key_s in candidates and best == candidates[key_s]:
                if key_s not in fallback_match or d < fallback_match[key_s][0]:
                    fallback_match[key_s] = (d, comp)
                if ssrec == 'WR' and (key_s not in best_match or d < best_match[key_s][0]):
                    best_match[key_s] = (d, comp)

            key_a = (eid, 'average', pid)
            if key_a in candidates and avg == candidates[key_a]:
                if key_a not in fallback_match or d < fallback_match[key_a][0]:
                    fallback_match[key_a] = (d, comp)
                if sarec == 'WR' and (key_a not in best_match or d < best_match[key_a][0]):
                    best_match[key_a] = (d, comp)

    log(f'Done. {count} lignes results.tsv parcourues pour EVENT_RECORDS.')

    output = {}
    all_event_ids = set(eid for eid, _, _ in candidates) | set(single_holders) | set(average_holders)
    for eid in all_event_ids:
        entry = {}
        for type_name, holders_map in (('single', single_holders), ('average', average_holders)):
            lst = holders_map.get(eid)
            if not lst:
                continue
            value = lst[0][1]
            holders_out = []
            for pid, val in lst:
                key = (eid, type_name, pid)
                match = best_match.get(key) or fallback_match.get(key)
                comp_id = match[1] if match else None
                if comp_id:
                    holders_out.append({'personId': pid, 'competitionId': comp_id})
            if holders_out:
                entry[type_name] = {'value': value, 'holders': holders_out}
        if entry:
            output[eid] = entry
    return output


# ---------------------------------------------------------------------------
# CHAMPIONSHIP_COMPS : competition_id -> liste de pays (ISO2) dont c'est le
# Championnat national officiel (championship_type = code ISO2 à 2 lettres).
# ---------------------------------------------------------------------------
def compute_championship_comps(tsv_dir):
    log('Calcul de CHAMPIONSHIP_COMPS...')
    comp_to_countries = defaultdict(list)
    for row in load_tsv(os.path.join(tsv_dir, 'championships.tsv')):
        ctype = row['championship_type']
        if len(ctype) == 2 and ctype.isupper() and ctype.isalpha():
            comp_to_countries[row['competition_id']].append(ctype)
    log(f'{len(comp_to_countries)} compétitions flaguées comme championnat national.')
    return dict(comp_to_countries)


# ---------------------------------------------------------------------------
# country_stats.json : podiums/records nationaux, cubeurs les plus titrés /
# podiumés / médaillés / prolifiques en records / assidus, par pays.
# ---------------------------------------------------------------------------
def compute_country_stats(tsv_dir, countries_by_name, name_to_iso2, iso2_to_name,
                           comp_to_countries, final_round_ids, comp_dates, format_sort_by):
    log('Calcul de country_stats.json...')

    iso2_comps = defaultdict(list)
    for comp_id, iso2_list in comp_to_countries.items():
        for iso2 in iso2_list:
            iso2_comps[iso2].append(comp_id)
    latest_champ_comp = {iso2: max(comps, key=lambda c: comp_dates.get(c, (0, 1, 1)))
                          for iso2, comps in iso2_comps.items()}
    latest_comp_to_iso2 = {comp: iso2 for iso2, comp in latest_champ_comp.items()}
    log(f'Dernière édition du championnat national déterminée pour {len(latest_champ_comp)} pays.')

    def earliest_comp(comp_list):
        if not comp_list:
            return None
        return min(comp_list, key=lambda c: comp_dates.get(c, (9999, 1, 1)))

    best_single = {}
    best_average = {}
    nr_count = defaultdict(int)
    title_wins = defaultdict(list)
    country_person_comps = defaultdict(set)
    podium_finish_count = defaultdict(int)
    gold_count = defaultdict(int)
    nc_podium_count = defaultdict(int)
    podium_candidates = defaultdict(lambda: defaultdict(list))

    log('PASS 1 sur results.tsv...')
    with open(os.path.join(tsv_dir, 'results.tsv'), encoding='utf-8') as f:
        r = csv.reader(f, delimiter='\t')
        header = next(r)
        idx = {h: i for i, h in enumerate(header)}
        count = 0
        for row in r:
            count += 1
            if count % 1000000 == 0:
                log(f'  pass1: {count} lignes...')
            pid = row[idx['person_id']]
            eid = row[idx['event_id']]
            best = row[idx['best']]
            avg = row[idx['average']]
            comp = row[idx['competition_id']]
            rtype = row[idx['round_type_id']]
            pos = row[idx['pos']]
            fmt = row[idx['format_id']]
            ssrec = row[idx['regional_single_record']]
            sarec = row[idx['regional_average_record']]
            cname = row[idx['person_country_id']]

            country_person_comps[(cname, pid)].add(comp)

            if ssrec == 'NR':
                nr_count[(cname, pid)] += 1
            if sarec == 'NR':
                nr_count[(cname, pid)] += 1

            bv = valid_time(best)
            if bv is not None:
                k = (cname, eid, pid)
                if k not in best_single or bv < best_single[k]:
                    best_single[k] = bv
            av = valid_time(avg)
            if av is not None:
                k = (cname, eid, pid)
                if k not in best_average or av < best_average[k]:
                    best_average[k] = av

            if comp in comp_to_countries and pos == '1' and rtype in final_round_ids:
                for iso2 in comp_to_countries[comp]:
                    title_wins[(iso2, pid)].append((eid, comp))

            if rtype in final_round_ids and pos.isdigit():
                posv = int(pos)
                if posv <= 3:
                    podium_finish_count[(cname, pid)] += 1
                if posv == 1:
                    gold_count[(cname, pid)] += 1
                if posv <= 3 and comp in comp_to_countries:
                    for iso2 in comp_to_countries[comp]:
                        if cname == iso2_to_name.get(iso2):
                            nc_podium_count[(iso2, pid)] += 1

            if comp in latest_comp_to_iso2 and rtype in final_round_ids and pos.isdigit():
                podium_candidates[latest_comp_to_iso2[comp]][eid].append(
                    (int(pos), pid, bv, av, fmt, cname))
    log(f'Done pass1. {count} lignes.')

    log('Construction des classements (tri avec égalités) par pays/épreuve...')

    def build_ranked(best_map):
        by_country_event = defaultdict(lambda: defaultdict(list))
        for (cname, eid, pid), val in best_map.items():
            by_country_event[cname][eid].append((pid, val))
        ranked = defaultdict(dict)
        for cname, events in by_country_event.items():
            iso2 = name_to_iso2.get(cname)
            if not iso2:
                continue
            for eid, lst in events.items():
                lst.sort(key=lambda x: x[1])
                out = []
                prev_val, prev_rank = None, 0
                for i, (pid, val) in enumerate(lst):
                    rank = (i + 1) if val != prev_val else prev_rank
                    out.append((rank, pid, val))
                    prev_val, prev_rank = val, rank
                ranked[iso2][eid] = out
        return ranked

    ranked_single = build_ranked(best_single)
    ranked_average = build_ranked(best_average)
    log(f'{len(ranked_single)} pays avec un classement single.')

    log('Construction des podiums de championnat (dernière édition par pays)...')
    final_podium = defaultdict(dict)
    for iso2, events in podium_candidates.items():
        cname_target = iso2_to_name.get(iso2)
        for eid, entries in events.items():
            filtered = [e for e in entries if e[5] == cname_target]
            filtered.sort(key=lambda e: e[0])
            out = []
            prev_pos, prev_rank = None, 0
            for i, (posv, pid, bv, av, fmt, cname2) in enumerate(filtered):
                rank = (i + 1) if posv != prev_pos else prev_rank
                sort_by = format_sort_by.get(fmt, 'average')
                is_average = sort_by == 'average' and av is not None
                value = av if is_average else bv
                out.append((rank, pid, value, is_average))
                prev_pos, prev_rank = posv, rank
            final_podium[iso2][eid] = [e for e in out if e[0] <= 3]

    nr_single_candidates = {}
    nr_average_candidates = {}
    for iso2, events in ranked_single.items():
        cname = iso2_to_name.get(iso2)
        for eid, lst in events.items():
            for rank, pid, val in lst:
                if rank == 1:
                    nr_single_candidates[(pid, eid, cname)] = val
    for iso2, events in ranked_average.items():
        cname = iso2_to_name.get(iso2)
        for eid, lst in events.items():
            for rank, pid, val in lst:
                if rank == 1:
                    nr_average_candidates[(pid, eid, cname)] = val

    log('PASS 2 sur results.tsv (recherche de la compétition de chaque record national)...')
    nr_single_matches = defaultdict(list)
    nr_average_matches = defaultdict(list)
    with open(os.path.join(tsv_dir, 'results.tsv'), encoding='utf-8') as f:
        r = csv.reader(f, delimiter='\t')
        header = next(r)
        idx = {h: i for i, h in enumerate(header)}
        count = 0
        for row in r:
            count += 1
            if count % 1000000 == 0:
                log(f'  pass2: {count} lignes...')
            pid = row[idx['person_id']]
            eid = row[idx['event_id']]
            best = row[idx['best']]
            avg = row[idx['average']]
            comp = row[idx['competition_id']]
            cname = row[idx['person_country_id']]

            k = (pid, eid, cname)
            bv = valid_time(best)
            if bv is not None and k in nr_single_candidates and nr_single_candidates[k] == bv:
                nr_single_matches[k].append(comp)
            av = valid_time(avg)
            if av is not None and k in nr_average_candidates and nr_average_candidates[k] == av:
                nr_average_matches[k].append(comp)
    log(f'Done pass2. {count} lignes.')

    log('Agrégation des statistiques par pays...')

    def top_by_country(counter):
        best = defaultdict(lambda: (None, 0))
        for (cname_or_iso2, pid), cnt in counter.items():
            iso2 = cname_or_iso2 if len(cname_or_iso2) == 2 else name_to_iso2.get(cname_or_iso2)
            if not iso2:
                continue
            if cnt > best[iso2][1]:
                best[iso2] = (pid, cnt)
        return best

    most_comps_by_country = defaultdict(lambda: (None, 0))
    for (cname, pid), comps in country_person_comps.items():
        iso2 = name_to_iso2.get(cname)
        if not iso2:
            continue
        cnt = len(comps)
        if cnt > most_comps_by_country[iso2][1]:
            most_comps_by_country[iso2] = (pid, cnt)

    most_nr_by_country = top_by_country(nr_count)
    most_podiums_by_country = top_by_country(podium_finish_count)
    most_golds_by_country = top_by_country(gold_count)
    most_nc_podiums_by_country = top_by_country(nc_podium_count)

    title_counts = defaultdict(lambda: defaultdict(list))
    for (iso2, pid), lst in title_wins.items():
        title_counts[iso2][pid] = lst
    most_titles_by_country = {}
    for iso2, pmap in title_counts.items():
        pid, lst = max(pmap.items(), key=lambda kv: len(kv[1]))
        most_titles_by_country[iso2] = (pid, len(lst), lst)

    log('Construction de la structure de sortie...')
    output = {}
    all_iso2 = (set(ranked_single.keys()) | set(most_comps_by_country.keys()) | set(most_nr_by_country.keys())
                | set(most_titles_by_country.keys()) | set(most_podiums_by_country.keys())
                | set(most_golds_by_country.keys()) | set(most_nc_podiums_by_country.keys()))
    for iso2 in all_iso2:
        cname = iso2_to_name.get(iso2)
        events_out = {}
        for eid, lst in ranked_single.get(iso2, {}).items():
            podium = [{'rank': rk, 'personId': p, 'value': v, 'isAverage': ia}
                      for (rk, p, v, ia) in final_podium.get(iso2, {}).get(eid, [])]
            top = next((e for e in lst if e[0] == 1), None)
            nr_single = None
            if top:
                _, pid, val = top
                comp = earliest_comp(nr_single_matches.get((pid, eid, cname), []))
                nr_single = {'personId': pid, 'value': val, 'competitionId': comp}
            events_out[eid] = {'podium': podium, 'nrSingle': nr_single}

        for eid, lst in ranked_average.get(iso2, {}).items():
            top = next((e for e in lst if e[0] == 1), None)
            events_out.setdefault(eid, {'podium': [], 'nrSingle': None})
            if top:
                _, pid, val = top
                comp = earliest_comp(nr_average_matches.get((pid, eid, cname), []))
                events_out[eid]['nrAverage'] = {'personId': pid, 'value': val, 'competitionId': comp}
            else:
                events_out[eid]['nrAverage'] = None

        entry = {'events': events_out}
        if iso2 in latest_champ_comp:
            entry['podiumCompetitionId'] = latest_champ_comp[iso2]
        if most_comps_by_country[iso2][0]:
            pid, cnt = most_comps_by_country[iso2]
            entry['mostComps'] = {'personId': pid, 'count': cnt}
        if most_nr_by_country[iso2][0]:
            pid, cnt = most_nr_by_country[iso2]
            entry['mostNR'] = {'personId': pid, 'count': cnt}
        if most_podiums_by_country[iso2][0]:
            pid, cnt = most_podiums_by_country[iso2]
            entry['mostPodiums'] = {'personId': pid, 'count': cnt}
        if most_golds_by_country[iso2][0]:
            pid, cnt = most_golds_by_country[iso2]
            entry['mostGolds'] = {'personId': pid, 'count': cnt}
        if most_nc_podiums_by_country[iso2][0]:
            pid, cnt = most_nc_podiums_by_country[iso2]
            entry['mostNCPodiums'] = {'personId': pid, 'count': cnt}
        if iso2 in most_titles_by_country:
            pid, cnt, lst = most_titles_by_country[iso2]
            entry['mostTitles'] = {'personId': pid, 'count': cnt,
                                    'titles': [{'event': e, 'competitionId': c} for e, c in lst]}
        output[iso2] = entry

    log(f'{len(output)} pays dans country_stats.json.')
    return output


# ---------------------------------------------------------------------------
# Écriture : remplace les constantes correspondantes dans index.html, et
# réécrit country_stats.json.
# ---------------------------------------------------------------------------
def replace_const_line(html, const_name, new_value_js):
    pattern = re.compile(r'^const ' + re.escape(const_name) + r' = .*;$', re.MULTILINE)
    new_line = f'const {const_name} = {new_value_js};'
    new_html, n = pattern.subn(new_line, html, count=1)
    if n != 1:
        raise RuntimeError(f"Impossible de trouver/remplacer 'const {const_name} = ...;' dans index.html")
    return new_html


def js_string(s):
    return json.dumps(s, ensure_ascii=False)


def compact_json(obj):
    return json.dumps(obj, separators=(',', ':'), sort_keys=True, ensure_ascii=False)


def patch_index_html(today_fr, event_totals, event_country_totals, event_continent_totals,
                      event_records, championship_comps):
    with open(INDEX_HTML_PATH, encoding='utf-8') as f:
        html = f.read()

    html = replace_const_line(html, 'EVENT_TOTALS_DATE', js_string(today_fr))
    html = replace_const_line(html, 'EVENT_TOTALS', compact_json(event_totals))
    html = replace_const_line(html, 'EVENT_COUNTRY_TOTALS', compact_json(event_country_totals))
    html = replace_const_line(html, 'EVENT_CONTINENT_TOTALS', compact_json(event_continent_totals))
    html = replace_const_line(html, 'EVENT_RECORDS_DATE', js_string(today_fr))
    html = replace_const_line(html, 'EVENT_RECORDS', compact_json(event_records))
    html = replace_const_line(html, 'CHAMPIONSHIP_COMPS', compact_json(championship_comps))
    html = replace_const_line(html, 'CAREER_STATS_DATE', js_string(today_fr))
    html = replace_const_line(html, 'COUNTRY_STATS_DATE', js_string(today_fr))

    with open(INDEX_HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(html)
    log('index.html mis à jour.')


def write_country_stats(country_stats):
    with open(COUNTRY_STATS_PATH, 'w', encoding='utf-8') as f:
        json.dump(country_stats, f, separators=(',', ':'), sort_keys=True, ensure_ascii=False)
    log('country_stats.json mis à jour.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tsv-dir', help='Dossier contenant déjà results.tsv, ranks_single.tsv, etc. '
                                           '(évite de re-télécharger l\'export). Si absent, téléchargement frais.')
    parser.add_argument('--dry-run', action='store_true',
                         help="Calcule tout mais n'écrit ni index.html ni country_stats.json.")
    args = parser.parse_args()

    tmp_dir = None
    try:
        if args.tsv_dir:
            tsv_dir = args.tsv_dir
            log(f'Réutilisation de l\'export déjà présent dans {tsv_dir}')
        else:
            tmp_dir = tempfile.mkdtemp(prefix='wca_export_')
            download_export(tmp_dir)
            tsv_dir = tmp_dir

        countries = load_tsv(os.path.join(tsv_dir, 'countries.tsv'))
        countries_by_name = {c['id']: c for c in countries}
        name_to_iso2 = {c['id']: c['iso2'] for c in countries}
        iso2_to_name = {c['iso2']: c['id'] for c in countries}

        round_types = load_tsv(os.path.join(tsv_dir, 'round_types.tsv'))
        final_round_ids = {r['id'] for r in round_types if r['final'] == '1'}
        log('Round types finaux :', final_round_ids)

        formats = load_tsv(os.path.join(tsv_dir, 'formats.tsv'))
        format_sort_by = {fmt['id']: fmt['sort_by'] for fmt in formats}

        comp_dates = {}
        cancelled_comps = set()
        with open(os.path.join(tsv_dir, 'competitions.tsv'), encoding='utf-8') as f:
            for row in csv.DictReader(f, delimiter='\t'):
                try:
                    comp_dates[row['id']] = (int(row['year']), int(row['month']), int(row['day']))
                except (ValueError, KeyError):
                    comp_dates[row['id']] = (9999, 1, 1)
                if row.get('cancelled') == '1':
                    cancelled_comps.add(row['id'])

        championship_comps = compute_championship_comps(tsv_dir)
        championship_comps = {cid: countries for cid, countries in championship_comps.items()
                               if cid not in cancelled_comps}

        event_totals, event_country_totals, event_continent_totals = compute_event_totals(tsv_dir, countries_by_name)
        event_records = compute_event_records(tsv_dir, comp_dates)
        country_stats = compute_country_stats(
            tsv_dir, countries_by_name, name_to_iso2, iso2_to_name,
            championship_comps, final_round_ids, comp_dates, format_sort_by,
        )

        today_fr = fr_date(datetime.now(timezone.utc).date())

        if args.dry_run:
            log('--dry-run : aucun fichier modifié.')
            log('EVENT_TOTALS (333):', event_totals.get('333'))
            log('EVENT_RECORDS (333):', event_records.get('333'))
            log('country_stats FR mostTitles:', country_stats.get('FR', {}).get('mostTitles'))
        else:
            patch_index_html(today_fr, event_totals, event_country_totals, event_continent_totals,
                              event_records, championship_comps)
            write_country_stats(country_stats)
            log('Synchronisation terminée avec succès (', today_fr, ').')
    finally:
        if tmp_dir:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == '__main__':
    main()
