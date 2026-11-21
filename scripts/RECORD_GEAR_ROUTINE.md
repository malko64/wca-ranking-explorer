# Recherche quotidienne du cube et des réglages des records du monde

Instructions suivies chaque jour par une tâche Claude planifiée (routine cloud), après la
synchronisation des données WCA (`.github/workflows/sync-wca-data.yml`, 03:00 UTC).

Objectif : pour chaque **nouveau** record du monde de `EVENT_RECORDS` (index.html), trouver le
cube utilisé et, si documentés, les réglages du cube, puis les enregistrer dans `RECORD_GEAR`
(index.html) et publier.

## 1. Préparer

```
git checkout main && git pull --rebase
```

## 2. Lister les records à chercher

Clé d'un record : `épreuve|single ou average|valeur|personId` (ex. `333|single|251|2023GENG02`).
Le script suivant affiche les clés à chercher (une par ligne, avec la compétition) :

```
node -e '
const fs=require("fs");const h=fs.readFileSync("index.html","utf8");
const R=JSON.parse(h.match(/^const EVENT_RECORDS = (\{.*\});$/m)[1]);
const G=h.match(/const RECORD_GEAR = \{([\s\S]*?)\n\};/)[1];
const known=new Map([...G.matchAll(/"([^"]+\|[^"]+)": \{([\s\S]*?)\}(,|\n)/g)].map(m=>[m[1],m[2]]));
const today=new Date();
for (const e in R){ if(e==="333fm") continue;
 for (const t of ["single","average"]){ const r=R[e][t]; if(!r) continue;
  for (const x of r.holders){ const k=`${e}|${t}|${r.value}|${x.personId}`; const v=known.get(k);
   if(!v){ console.log("NEW", k, x.competitionId); continue; }
   const d=(v.match(/searched: "([^"]+)"/)||[])[1];
   if(!/cube:/.test(v) && !/retried/.test(v) && d && (today-new Date(d))/864e5>=30) console.log("RETRY", k, x.competitionId);
 }}}'
```

- `NEW` : record jamais cherché.
- `RETRY` : cherché il y a 30 jours ou plus sans rien trouver ; on retente **une seule fois**
  (les articles sur un record paraissent souvent après coup).

**S'il n'y a aucune ligne : arrêter ici, sans commit.**

## 3. Chercher (pour chaque record listé)

1. Nom du cubeur : `curl -s https://www.worldcubeassociation.org/api/v0/persons/<personId>`.
2. Faire **au moins 4 recherches web** variées, par exemple :
   - `<nom> <temps> <épreuve> world record cube`
   - `<nom> <épreuve> world record <compétition> puzzle used`
   - blogs des marques : gancube.com, moyu / speedcubeshop, qiyi, dayan, thecubicle, kewbz
   - speedcubing.org, speedsolving.com (forum), Guinness, titres de vidéos YouTube
   - pour un cubeur chinois, aussi en chinois (`<nom chinois> 世界纪录 魔方 型号`)
   - réglages : `<nom> <cube> setup tension magnets lube`
3. Ouvrir les pages prometteuses pour vérifier ce qu'elles disent réellement.

Règles de fiabilité (**ne jamais inventer**) :
- `cube` : seulement si une source relie ce modèle à **ce record précis** (ou à cette compétition).
  Si on ne trouve que le « cube principal » habituel du cubeur à cette période, on peut l'indiquer
  avec `unsure: true`. Sinon, ne pas mettre de `cube`.
- `setup` : seulement des réglages explicitement donnés pour le cube de ce record (tension,
  centre, aimants, lubrifiant…). Un réglage « général » du cubeur → ne pas l'indiquer.
- `sources` : uniquement des URL réellement trouvées/ouvertes, avec un libellé court (nom du site).
  Plusieurs sources indépendantes qui concordent = plusieurs entrées dans `sources`.

## 4. Écrire dans `RECORD_GEAR` (index.html)

Format (garder l'ordre et le style des entrées existantes) :

```js
  "333|single|251|2023GENG02": {
    searched: "AAAA-MM-JJ",
    cube: "GAN 16 MagLev MAX",
    unsure: true,               // seulement si attribution incertaine
    sources: [{ label: "SpeedSolving wiki", url: "https://..." }],
    setup: { text: "Tension 4, centre 3, aimants forts", sources: [{ label: "...", url: "https://..." }] },
  },
```

- Rien trouvé : `"<clé>": { searched: "AAAA-MM-JJ" },`
- Rien trouvé lors d'un `RETRY` : `"<clé>": { searched: "AAAA-MM-JJ", retried: true },`
- Supprimer les entrées dont la clé n'existe plus dans `EVENT_RECORDS` (record battu).
- Ne modifier **que** le bloc `RECORD_GEAR`.

Vérifier la syntaxe :

```
node -e 'const h=require("fs").readFileSync("index.html","utf8");for(const m of h.matchAll(/<script>([\s\S]*?)<\/script>/g)) new Function(m[1]);console.log("ok")'
```

## 5. Publier

```
git add index.html
git commit -m "Recherche cube/réglages des nouveaux records du monde"
git pull --rebase && git push origin main
```

Terminer par un court résumé : records traités, cube trouvé ou non, sources.
