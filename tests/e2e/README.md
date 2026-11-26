# Prueba del reproductor en un navegador real (opcional)

`player_e2e.py` levanta la app con una base temporal, un "CDN" local con soporte de Range y CORS,
y recorre la ficha en Chromium headless: selectores de idioma/calidad/subtítulos (arriba y en la
barra de Video.js), cambio de versión conservando la posición, HLS con dos pistas de audio, CSP
estricta sin violaciones y la vista de temporadas/capítulos.

```bash
pip install playwright && playwright install chromium
export RASCAL_E2E_DIR=/tmp/pw && mkdir -p $RASCAL_E2E_DIR/media/hls && cd $RASCAL_E2E_DIR
npm install video.js@8.10.0
cd media
ffmpeg -y -f lavfi -i testsrc=size=640x360:rate=24 -f lavfi -i sine=frequency=440 -t 12 -c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart es.mp4
ffmpeg -y -f lavfi -i testsrc2=size=640x360:rate=24 -f lavfi -i sine=frequency=880 -t 12 -c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart en.mp4
ffmpeg -y -f lavfi -i testsrc=size=640x360:rate=24 -f lavfi -i sine=frequency=300 -f lavfi -i sine=frequency=600 -t 12 \
  -map 0:v -map 1:a -map 2:a -c:v libx264 -pix_fmt yuv420p -c:a aac -f hls -hls_time 4 -hls_playlist_type vod \
  -master_pl_name master.m3u8 -var_stream_map "v:0,agroup:aud a:0,agroup:aud,language:spa,name:Español,default:yes a:1,agroup:aud,language:jpn,name:Japonés" \
  -hls_segment_filename 'hls/s_%v_%03d.ts' hls/stream_%v.m3u8
cd - && RASCAL_ALLOW_PRIVATE_URLS=true python tests/e2e/player_e2e.py
```

Imprime un JSON con los resultados y deja capturas en `$RASCAL_E2E_DIR`.
