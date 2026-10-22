"""
Bite Club - gera a lista de restaurantes, bares e cafes de Sao Paulo
a partir da base aberta do Overture Maps (dados de Meta, Microsoft, Foursquare e outros).

Este arquivo roda sozinho no GitHub Actions (veja .github/workflows/atualizar.yml).
Ninguem precisa editar nada aqui para ele funcionar.

Saidas (pasta data/):
  - restaurantes_sp_ab.csv  -> so os bairros do publico A/B (importe este primeiro)
  - restaurantes_sp.csv     -> a cidade inteira
  - RESUMO.md               -> quantos lugares vieram, por bairro
"""

import datetime
import os
import unicodedata

import duckdb

PLACES_FILE = "places.parquet"
DIVISIONS_FILE = "divisions.parquet"
OUT_DIR = "data"

# Lugares com confianca menor que isso costumam estar fechados ou errados.
MIN_CONFIDENCE = 0.6

# Bairros/distritos do publico A/B (acentos e maiusculas nao importam).
BAIRROS_AB = [
    "Pinheiros", "Alto de Pinheiros", "Itaim Bibi", "Jardim Paulista", "Jardins",
    "Jardim Europa", "Jardim America", "Cerqueira Cesar", "Moema", "Vila Nova Conceicao",
    "Vila Olimpia", "Vila Madalena", "Higienopolis", "Consolacao", "Perdizes",
    "Brooklin", "Campo Belo", "Vila Mariana", "Paraiso", "Morumbi", "Cidade Jardim",
]

# Palavras que indicam comida e bebida nas categorias do Overture.
INCLUIR = [
    "eat_and_drink", "restaurant", "bar", "pub", "cafe", "coffee", "bakery", "steakhouse",
    "bistro", "brewery", "gastropub", "tavern", "brasserie", "diner", "eatery", "creperie",
    "patisserie", "food_truck", "food_court", "ice_cream", "dessert", "juice", "tea_room",
    "bubble_tea", "cafeteria", "buffet", "pizzeria",
]

# Categorias que parecem comida, mas nao sao.
EXCLUIR = [
    "internet_cafe", "barber", "bar_supply", "restaurant_supply", "coffee_supplier",
    "food_bank", "pet_cafe",
]

# Pega o primeiro link de Instagram da lista de redes sociais (sem usar lambda).
INSTAGRAM_EXPR = (
    r"nullif(regexp_extract(array_to_string(socials, ' '), "
    r"'https?://(www\.)?instagram\.com/[^ ]+'), '')"
)


def normalizar(texto):
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return sem_acento.lower().strip()


def regex(palavras):
    return r"(^|[ _])(" + "|".join(palavras) + r")([ _]|$)"


con = duckdb.connect()
con.execute("INSTALL spatial")
con.execute("LOAD spatial")
con.execute(f"CREATE TABLE p AS SELECT * FROM read_parquet('{PLACES_FILE}')")
con.execute(f"CREATE TABLE draw AS SELECT * FROM read_parquet('{DIVISIONS_FILE}')")


def existe(tabela, expressao):
    """Testa se uma coluna/expressao existe nesta versao dos dados do Overture."""
    try:
        con.execute(f"SELECT {expressao} FROM {tabela} LIMIT 0")
        return True
    except duckdb.Error:
        return False


def campo(tabela, expressao, padrao="NULL"):
    return expressao if existe(tabela, expressao) else padrao


def geometria(tabela):
    """A geometria pode vir como GEOMETRY ou como bytes (WKB), dependendo da versao."""
    linha = con.execute(f"SELECT typeof(geometry) FROM {tabela} LIMIT 1").fetchone()
    if linha and linha[0].upper().startswith("GEOMETRY"):
        return "geometry"
    return "ST_GeomFromWKB(geometry)"


total_bruto = con.execute("SELECT count(*) FROM p").fetchone()[0]
print(f"Lugares baixados (todos os tipos): {total_bruto}")
if total_bruto == 0:
    raise SystemExit("O download do Overture veio vazio. Rode de novo mais tarde.")

# ---------------------------------------------------------------- categorias
partes = [
    e for e in [
        "taxonomy.primary",
        "array_to_string(taxonomy.hierarchy, ' ')",
        "categories.primary",
        "basic_category",
    ]
    if existe("p", e)
]
if not partes:
    raise SystemExit("Nao achei nenhuma coluna de categoria no arquivo do Overture.")
texto_categoria = "lower(concat_ws(' ', " + ", ".join(partes) + "))"

exibir = [e for e in ["taxonomy.primary", "categories.primary", "basic_category"] if existe("p", e)]
categoria = "coalesce(" + ", ".join(exibir) + ")"

# ---------------------------------------------------------------- restaurantes
nome = "names.primary"
confianca = campo("p", "confidence", "1.0")

con.execute(f"""
CREATE TABLE food AS
SELECT
    CAST(id AS VARCHAR)                          AS overture_id,
    {nome}                                       AS name,
    {categoria}                                  AS category,
    {campo("p", "addresses[1].freeform")}        AS address,
    {campo("p", "addresses[1].postcode")}        AS postcode,
    {campo("p", "phones[1]")}                    AS phone,
    {campo("p", "websites[1]")}                  AS website,
    {campo("p", INSTAGRAM_EXPR)}                 AS instagram,
    round(CAST({confianca} AS DOUBLE), 2)        AS confidence,
    {geometria("p")}                             AS geom
FROM p
WHERE {nome} IS NOT NULL
  AND CAST({confianca} AS DOUBLE) >= {MIN_CONFIDENCE}
  AND regexp_matches({texto_categoria}, '{regex(INCLUIR)}')
  AND NOT regexp_matches({texto_categoria}, '{regex(EXCLUIR)}')
""")
print("Comida e bebida (area retangular):", con.execute("SELECT count(*) FROM food").fetchone()[0])

# ---------------------------------------------------------------- bairros e limite da cidade
pais = campo("draw", "country")
con.execute(f"""
CREATE TABLE d AS
SELECT names.primary AS name, subtype, {pais} AS country, {geometria("draw")} AS geom
FROM draw
WHERE names.primary IS NOT NULL
""")

con.execute("""
CREATE TABLE sp AS
SELECT geom FROM d
WHERE subtype = 'locality'
  AND lower(strip_accents(name)) = 'sao paulo'
  AND coalesce(country, 'BR') = 'BR'
""")
if con.execute("SELECT count(*) FROM sp").fetchone()[0] > 0:
    con.execute("DELETE FROM food WHERE NOT EXISTS (SELECT 1 FROM sp WHERE ST_Intersects(sp.geom, food.geom))")
    print("Dentro do municipio de Sao Paulo:", con.execute("SELECT count(*) FROM food").fetchone()[0])
else:
    print("Aviso: limite do municipio nao encontrado; mantendo a area retangular.")

con.execute("""
CREATE TABLE hoods AS
SELECT name, geom, ST_Area(geom) AS area
FROM d
WHERE subtype IN ('microhood', 'neighborhood', 'macrohood', 'borough', 'localadmin')
""")
con.execute("""
CREATE TABLE bairro AS
SELECT f.overture_id, arg_min(h.name, h.area) AS neighborhood
FROM food f
JOIN hoods h ON ST_Intersects(h.geom, f.geom)
GROUP BY f.overture_id
""")
con.execute("""
CREATE TABLE final AS
SELECT f.*, b.neighborhood
FROM food f
LEFT JOIN bairro b USING (overture_id)
""")

# ---------------------------------------------------------------- arquivos
os.makedirs(OUT_DIR, exist_ok=True)

colunas = """
    overture_id, name, category, neighborhood, address, postcode,
    'São Paulo' AS city,
    round(ST_Y(geom), 6) AS latitude,
    round(ST_X(geom), 6) AS longitude,
    phone, website, instagram, confidence
"""
lista_ab = ", ".join("'" + normalizar(b).replace("'", "''") + "'" for b in BAIRROS_AB)
filtro_ab = f"lower(strip_accents(neighborhood)) IN ({lista_ab})"

con.execute(f"""
COPY (SELECT {colunas} FROM final ORDER BY neighborhood NULLS LAST, name)
TO '{OUT_DIR}/restaurantes_sp.csv' (HEADER, DELIMITER ',')
""")
con.execute(f"""
COPY (SELECT {colunas} FROM final WHERE {filtro_ab} ORDER BY neighborhood, name)
TO '{OUT_DIR}/restaurantes_sp_ab.csv' (HEADER, DELIMITER ',')
""")

total = con.execute("SELECT count(*) FROM final").fetchone()[0]
total_ab = con.execute(f"SELECT count(*) FROM final WHERE {filtro_ab}").fetchone()[0]
sem_bairro = con.execute("SELECT count(*) FROM final WHERE neighborhood IS NULL").fetchone()[0]
por_bairro = con.execute("""
SELECT coalesce(neighborhood, '(sem bairro)') AS b, count(*) AS n
FROM final GROUP BY 1 ORDER BY n DESC
""").fetchall()

hoje = datetime.date.today().isoformat()
linhas = [
    f"# Restaurantes de São Paulo — atualizado em {hoje}",
    "",
    "Fonte: Overture Maps Foundation (dados abertos).",
    "",
    f"- Total na cidade: **{total}**",
    f"- Nos bairros A/B (restaurantes_sp_ab.csv): **{total_ab}**",
    f"- Sem bairro identificado: **{sem_bairro}**",
    "",
    "| Bairro | Lugares |",
    "| --- | --- |",
]
linhas += [f"| {b} | {n} |" for b, n in por_bairro]
with open(f"{OUT_DIR}/RESUMO.md", "w", encoding="utf-8") as arquivo:
    arquivo.write("\n".join(linhas) + "\n")

print(f"Total na cidade: {total} | Bairros A/B: {total_ab} | Sem bairro: {sem_bairro}")
if total == 0:
    raise SystemExit("Nenhum restaurante encontrado. Algo mudou no formato do Overture.")
