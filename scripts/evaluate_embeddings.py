"""Bounded synthetic bilingual retrieval check, not a representative accuracy benchmark."""
import argparse
import json
import resource
import statistics
import time
import numpy as np
from argon_recordings.embeddings import Embeddings

DOCUMENTS = [
 'Acordamos restaurar PostgreSQL desde la copia de seguridad antes de volver a habilitar escrituras.',
 'El problema del inicio de sesión ocurre cuando caduca la cookie. La solución es renovar la sesión.',
 'La animación del menú lateral se corta en Safari. Hay que revisar la transición en el teléfono.',
 'We will send invitations only after the administrator assigns a reviewer role to the new member.',
 'The search index must be rebuilt when someone accepts an edited transcript.',
 'Las facturas de este mes vencen el viernes. Finanzas confirmará los pagos pendientes.',
 'The video can lose frames during compression, so keep the original for reviewing animations.',
 'El trabajador usa Turbo con dos hilos de CPU. Los trabajos urgentes tienen prioridad en la cola.',
 'Redis is a cache, while PostgreSQL stores the durable project records.',
 'Para corregir nombres técnicos, el usuario marca una palabra y proporciona una pista al transcriptor.',
 'Audio timestamps must preserve the silent gap before speech starts, otherwise screenshots are misaligned.',
 'Vamos a reducir el consumo de memoria descargando los modelos que no estén en uso.',
 'Password reset emails expire after ten minutes and can only be used once.',
 'The export includes a transcript, visual references, and metadata in a ZIP archive.',
 'Se canceló el despliegue porque la migración dejó la base de datos en modo de solo lectura.',
 'We measured a slow database query and added an index to the recording creation timestamp.',
]
QUERIES = [
 ('Where did we discuss recovering the database from backup?', 0),
 ('¿Cómo arreglamos el acceso cuando expira la sesión?', 1),
 ('Where was the broken mobile sidebar motion mentioned?', 2),
 ('¿Quién da permiso a los nuevos invitados?', 3),
 ('¿Qué pasa con la búsqueda después de corregir la transcripción?', 4),
 ('When do we need to pay the outstanding bills?', 5),
 ('¿Por qué conservar el vídeo original si ya tenemos uno comprimido?', 6),
 ('How many processor threads does transcription use?', 7),
 ('¿Qué guardamos en caché y qué guardamos de forma persistente?', 8),
 ('How do we fix a misspelled technical term in the transcript?', 9),
 ('¿Por qué no coinciden las capturas con el momento del audio?', 10),
 ('Where did we discuss freeing RAM?', 11),
 ('¿Cuánto dura el enlace para recuperar la contraseña?', 12),
 ('What files are bundled in the downloadable recording report?', 13),
 ('Why did we stop the release when writes stopped working?', 14),
 ('¿Cómo aceleramos las consultas sobre la fecha de las grabaciones?', 15),
]
parser = argparse.ArgumentParser()
parser.add_argument('directory')
parser.add_argument('--profile', choices=['small', 'base', 'base-fp32', 'large'], default='large')
args = parser.parse_args()
model = Embeddings(args.directory, args.profile)
start = time.monotonic()
model.encode(['warm up'], query=True)
load_seconds = time.monotonic() - start
start = time.monotonic()
documents = np.array(model.encode(DOCUMENTS))
index_seconds = time.monotonic() - start
ranks, details, latency = [], [], []
for query, expected in QUERIES:
    start = time.monotonic()
    vector = model.encode([query], query=True)[0]
    latency.append(time.monotonic() - start)
    order = np.argsort(documents @ vector)[::-1].tolist()
    rank = order.index(expected) + 1
    ranks.append(rank)
    details.append(dict(query=query, expected=expected, top=order[0], rank=rank))
# A long single ASR passage must preserve its tail in token-aware overlapping windows.
rows = [dict(id='tail', start=0, end=100, text=('palabra English ' * 500) + 'UNIQUE_TAIL')]
windows = model.chunks(rows)
assert 'UNIQUE_TAIL' in windows[-1]['text']
assert all(len(model.tokenizer.encode('passage: ' + w['text']).ids) < 256 for w in windows)
print(json.dumps(dict(profile=args.profile, queries=len(QUERIES), recall_at_1=sum(r == 1 for r in ranks)/len(ranks),
 recall_at_3=sum(r <= 3 for r in ranks)/len(ranks), load_seconds=load_seconds, index_seconds=index_seconds,
 query_median_seconds=statistics.median(latency), peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
 long_passage_tail_covered=True, details=details), ensure_ascii=False, indent=2))
