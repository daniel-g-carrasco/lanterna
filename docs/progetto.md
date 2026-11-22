# Lanterna: documento di progetto

Stato: **in sviluppo, nessuna release**. Data: 2026-11-03. Nome: **Lanterna**, dalla lanterna magica, l'antenata del proiettore: porta foto, presentazioni e film dal portatile al muro grande; sta accanto a Lucciola, sempre una luce. App ID `io.github.daniel_g_carrasco.lanterna`.

Legenda delle fonti: **[V]** verificato (sorgente, documentazione primaria o prova su questa macchina), **[I]** dedotto, da confermare con una prova. Le prove e i comandi stanno nell'appendice A.

## 0. In breve

- **Cosa fa**: mostra sulla TV, attraverso il Chromecast che la TV ha dentro, le foto, le presentazioni, i video e i film del portatile. Due modi: **un file** (la TV lo riceve com'è e lo decodifica lei; si ricodifica solo ciò che la TV non conosce) e **lo schermo o una finestra** (per le presentazioni e ciò che non è un file). Non serve a usare il desktop dalla TV: il ritardo del mirroring è di circa 2,7 secondi [V].
- **Motore**: GStreamer per leggere, ricodificare e catturare (con la codifica H.264 della GPU via VAAPI); il portale ScreenCast del sistema per lo schermo; Avahi per trovare le TV. Scritto da noi solo il necessario del protocollo Cast v2 (sei campi protobuf e una manciata di messaggi JSON), per non portarsi dietro una libreria protobuf.
- **Protezioni**: l'app non scrive mai sui file dell'utente: li legge. Il rischio vero è la riservatezza: mentre trasmette, un server HTTP risponde sulla rete di casa. Barriere: un solo elemento alla volta, dietro un token casuale; risposte solo all'indirizzo della TV scelta (e al computer stesso); niente più risposte appena l'elemento cambia o la trasmissione finisce.
- **App**: Python, GTK 4 + libadwaita, `Adw.Application` a istanza unica; Flatpak su Linux dal repository su GitHub Pages. Windows: vedi sezione 1.

## 1. Obiettivi e limiti

Obiettivi:
- mandare un film alla TV con la qualità del file e il salto avanti e indietro, anche quando il suono è AC-3 o DTS (si ricodifica solo il suono);
- mostrare foto e presentazioni, anche da niri (Lucciola), dove le altre app non funzionano (appendice A);
- integrarsi in Lucciola: una riga nella pagina Apps di Lucciola System (installa e rimuove il Flatpak) e un plugin Noctalia da subito (indicatore e tile), con MPRIS per i controlli della riproduzione (widget media, tasti multimediali, KDE Connect dal telefono). Decisioni del 25/10/2026, appendice C.

Fuori dal progetto, almeno all'inizio:
- **Windows**: il modo "schermo" passa dal portale Wayland e non ha controparte; il modo "file" si potrebbe portare (GStreamer e Avahi o un mDNS proprio), da valutare dopo la 0.1. Motivo scritto, come chiede la regola "Windows alla pari".
- playlist, sottotitoli, ricevitori diversi dal Default Media Receiver, il Cast Streaming a bassa latenza (Open Screen) per usare il desktop dalla TV.

## 2. Il punto di partenza

- La TV: Philips 55OLED806/12, Android TV 11, Chromecast integrato `cast_build_revision 3.72`, nome "Living Room TV", su Ethernet [V 25/10/2026]. Niente Miracast: GNOME Network Displays ha cercato in Wi-Fi Direct senza trovarla [V].
- Il portatile: Lucciola (niri 26.04, Noctalia), Radeon 760M con `vah264enc` e `vapostproc` sia sull'host (GStreamer 1.28.7) sia nel runtime GNOME 49 [V]. Zona firewall FedoraWorkstation con le porte 1025-65535 aperte [V].
- Le alternative provate: GNOME Network Displays 0.99 (Flathub) trova la TV ma non trasmette su niri; Chromium trasmetterebbe una scheda, ma non lo voglio usare.

## 3. Architettura

Logica senza GTK, ciascuna con i suoi test:
- `castv2.py`: il protocollo Cast v2. `Channel` (messaggi incorniciati su un socket TLS), `Receiver` (thread di lettura, battito ogni 5 s, la TV data per persa dopo 16 s di silenzio, richieste che aspettano la propria risposta, LAUNCH del Default Media Receiver, LOAD, PLAY, PAUSE, SEEK, STOP, volume).
- `server.py`: il server HTTP da cui la TV legge (vedi Protezioni); byte range per i file, un produttore per ogni GET per i flussi dal vivo.
- `media.py`: cosa contiene un file (il discoverer di GStreamer) e il **piano**, una lista ordinata di modi: DIRECT (il file com'è), AUDIO (immagine copiata, suono ricodificato), TRANSCODE (tutto ricodificato). Se la TV rifiuta un modo si prova il successivo.
- `devices.py`: le TV in rete, da Avahi sul bus di sistema.

Gli altri moduli: `transcode.py`, `session.py`, `playlist.py`, `screencast.py`, `keepawake.py`, la finestra (appendici E e F). Da fare: MPRIS, l'interfaccia `Cast1` con il file di stato per il plugin Noctalia, il manifest Flatpak.

## 4. Rischi e domande aperte

| Rischio | Mitigazione |
|---|---|
| Un altro dispositivo della rete legge i file del portatile | Token casuale per elemento, solo l'IP della TV e il loopback, nient'altro che l'elemento pubblicato, server spento a fine trasmissione (`test_server.py`) |
| Il portatile si sospende a metà film: la TV non legge più il file | Blocco della sospensione durante ogni trasmissione (logind dall'app; in Lucciola anche Keep Awake dal plugin); lo schermo può spegnersi durante un file, non durante il mirroring |
| Le notifiche compaiono sulla TV durante una presentazione | "Non disturbare" automatico durante il mirroring (lato Lucciola) |
| Un film che la TV rifiuta lascia l'utente senza spiegazione | Il piano prova il modo successivo e dice perché; i messaggi in parole, non in codici |
| La codifica H.264 manca su altre GPU (Intel, NVIDIA) o nel Flatpak | Da verificare con `vainfo` nel Flatpak [I]; ricaduta su un encoder software se presente |

Domande aperte: nessuna per ora (le decisioni del 25/10 sono nell'appendice C).

## Appendice A. Verifiche fatte

**25/10/2026, la prova di mirroring su niri** (`~/scratch/cast-poc/castpoc.py`, Python + PyGObject + GStreamer dell'host):
- portale ScreenCast → `pipewiresrc` → `vapostproc` → `vacompositor` → `vah264enc` → Matroska su HTTP → LOAD sul Default Media Receiver: **funziona** [V]; 29,8 fps costanti, un fotogramma chiave al secondo, 6,6 Mbit/s; ritardo misurato con due orologi in un video del telefono: TV 19:17:36.118, portatile 19:17:38.82, **circa 2,7 s** [V]; un BUFFERING isolato in qualche minuto.
- **niri consegna lo schermo solo come DMA-BUF** [V]: una catena in memoria di sistema (`videoconvert`) fallisce con "no more input formats", lo stesso errore di GNOME Network Displays e di vokoscreenNG #392.
- **La connessione D-Bus del portale va tenuta viva** [V]: se l'oggetto che la tiene viene liberato, `g_bus_get_sync` chiude il singleton, il portale chiude la sessione e niri registra `stop` 10-40 ms dopo `start` (journal di niri).
- **`pipewiresrc` 1.28: `path` è deprecato e `target-object` vuole l'`object.serial`**, non l'id del nodo che il portale restituisce [V]: con l'id, "target not found"; il serial si legge con `pw-dump`.
- **niri manda fotogrammi solo quando lo schermo cambia** [V]: un fotogramma in 6 s a schermo fermo; `keepalive-time` di `pipewiresrc` non aiuta con DMA-BUF; un `vacompositor` con uno sfondo nero dal vivo a 30 fps come metronomo dà un flusso costante, tutto sulla GPU.
- Il token del portale (`persist_mode` 2) evita il selettore dal secondo avvio [V].
- niri 26.04 espone `zwlr_screencopy_manager_v1` v3 ma non `ext-image-copy-capture` [V, registro Wayland letto con uno script].

**25/10/2026, i moduli sulla TV vera** (script di prova con un MP4 H.264+AAC generato da GStreamer, 1280×720, 21,85 s):
- scoperta con Avahi: "Living Room TV", `2021/22 Philips UHD Android TV`, 192.168.1.20:8009 [V];
- piano DIRECT `video/mp4`; LAUNCH, LOAD, PLAYING dopo **0,4 s**, PAUSE a 3,5 s, SEEK a 12 s, PLAY, STOP dell'app; 12,7 MB serviti dal server [V].
- La TV risponde a LAUNCH con `LAUNCH_STATUS` e `"status": "USER_ALLOWED"` (una stringa), e l'app compare in un RECEIVER_STATUS successivo [V]: il codice lo gestisce e il test lo riproduce.
- Il discoverer di GStreamer chiama lo stream di un WAV `audio/x-wav`, come il contenitore [V].

## Appendice B. Fonti principali

- Google, "Supported media for Google Cast" (formati del Default Media Receiver).
- Open Screen Library, `cast/README.md` e `cast/docs/external_libraries.md` (Cast Streaming, per il dopo).
- GNOME Network Displays 0.99 (Flathub) e il suo log di debug: la sequenza CONNECT, GET_STATUS, LAUNCH CC1AD845, CONNECT al transport, LOAD con `video/x-matroska` LIVE.
- Il plugin Noctalia e lo `status.py` di Spola negli extras di app-template (lo schema file di stato più gdbus).

## Appendice C. 25/10/2026: le decisioni sull'integrazione con Lucciola

Le decisioni prese:
- Riga nella pagina Apps di Lucciola System come per Spola, installazione per utente, non di serie; la ricetta confronta la chiave del flatpakref con un'impronta scritta nell'immagine (da fare anche per Spola).
- Plugin Noctalia da subito, primo perimetro indicatore e tile (il launcher dopo). La fonte sta nei sorgenti di Lanterna e si installa **dentro il Flatpak**; Lucciola aggiunge una sorgente di plugin che punta lì (da verificare che Noctalia accetti un percorso nella home e ignori una cartella assente) e mette indicatore e tile nei suoi default. Ripiego: copia fissata nell'immagine.
- **MPRIS** per la riproduzione dei file; `Cast1` solo per dispositivi e mirroring.
- File di stato in `$XDG_RUNTIME_DIR/app/io.github.daniel_g_carrasco.lanterna/` (in memoria, sparisce al logout), con la posizione, l'istante a cui si riferisce e la velocità, invece di scriverla ogni secondo.
- Metodi di `Cast1`: anche Seek, il volume della TV, Next e Previous per le presentazioni, Rescan. `StartMirror` risponde subito e il risultato arriva nello stato (il selettore del portale può durare più dei 25 s di timeout di gdbus).
- Keep Awake automatico durante **ogni** trasmissione (anche un film: la TV legge il file dal portatile); lo schermo spento va bene durante un file, non durante il mirroring; "non disturbare" automatico solo nel mirroring.
- Piano di lavoro: prima Lanterna fino alla prima release con il repository Flatpak e l'impronta della chiave; poi Lucciola con ricetta, riga Apps, plugin nei default, prove sul laptop. Il punto d'incontro è un documento del contratto `Cast1` in questo repository.

## Appendice D. 25/10/2026: i primi quattro moduli

Fatto: `castv2.py`, `server.py`, `media.py`, `devices.py`, con 15 + 14 + 14 + 5 test; la suite intera passa (95 test, 6 saltati dove manca Windows), ruff pulito; quattro commit. La prova d'insieme sulla TV è nell'appendice A.

Resta: le catene AUDIO e TRANSCODE (e la ricaduta automatica quando la TV rifiuta un modo), il mirroring con l'audio, la sessione, MPRIS, `Cast1` e il file di stato con il documento del contratto, il blocco della sospensione, la finestra, l'icona (con le anteprime prima del commit), il manifest Flatpak con i permessi (`--system-talk-name=org.freedesktop.Avahi`, la rete, `--talk-name=org.freedesktop.portal.*` già di serie), la verifica di `vainfo` nel Flatpak, il primo rilascio.

## Appendice E. 25/10/2026: la ricodifica dal vivo e la sessione

Fatto:
- `transcode.py`: i modi AUDIO (immagine copiata con `parsebin`, primo suono in Opus) e TRANSCODE (H.264 con VAAPI quando c'è, ridotto entro 1920×1080), in Matroska scritto direttamente nella risposta alla TV; avvio da un punto qualsiasi. 8 test con file veri fatti sul momento (VP8 + AC-3 in MKV, MPEG-4 + MP3 in AVI).
- `session.py`: una TV e ciò che mostra; il piano provato modo per modo, con il motivo quando la TV rifiuta; la posizione contata da noi nei flussi dal vivo (la TV dice 0); il salto in un flusso dal vivo come nuovo flusso da quel punto; foto con avanti e indietro. 12 test con la finta TV (`tests/_fake_tv.py`, ora in comune).

Scoperto [V]:
- `matroskamux` lascia fuori una traccia il cui pad viene chiesto dopo il primo buffer: i pad del muxer si chiedono prima che partano i dati.
- Un salto con flush dopo che il muxer ha scritto l'intestazione la perde, e `matroskamux` non la riscrive: fino al salto le uscite del demuxer scartano i dati (bloccarle invece ferma il thread unico del demuxer prima che mostri la seconda traccia).
- `parsebin` non emette mai `no-more-pads` (`decodebin` sì): si contano le tracce annunciate dalla lettura del file.
- Il salto va mandato dentro il demuxer da una sua uscita: mandato alla pipeline risale dall'appsink e si ferma al muxer.
- Sulla TV: un H.264 + AC-3 in Matroska parte in 0,7 s e di nuovo da 15 s in 0,3 s; una foto passa da IDLE a **PAUSED** e resta a schermo; la sessione vera mostra il film AC-3 in 3,3 s (connessione compresa), la posizione segue il tempo, il salto a 20 s riparte da lì.
- Un MKV H.264 + Opus mandato dal vivo arriva in fondo (FINISHED), e l'audio Opus si sente sulla TV [V, confermato a orecchio il 25/10/2026].

Resta: MPRIS, `Cast1` e il file di stato con il documento del contratto, il blocco della sospensione, il mirroring con l'audio (dalla prova), la finestra, l'icona, il manifest Flatpak, `vainfo` nel Flatpak, la prima release.

## Appendice F. 03/11/2026: la finestra, il mirroring, la sospensione, le prove sulla TV

Fatto:
- **Finestra** (`window.py`): le TV da Avahi in un menu in testata, con la TV usata l'ultima volta; file e cartelle dal selettore, trascinati o dalla riga di comando, in attesa se la TV non è ancora stata trovata; pausa, salto dove la barra si ferma, precedente e successivo, volume della TV, stop; spazio e frecce. Il campione del template (`counting.py`) è sparito.
- **`playlist.py`**: una cartella sono le sue foto e i suoi video nell'ordine umano; una foto sola si porta dietro la sua cartella; un video solo resta solo.
- **`screencast.py`**: il mirroring della prova, con il suono dell'uscita predefinita mescolato a silenzio dal vivo. **Il selettore chiede ogni volta**: niente token del portale, perché cosa mostrare sulla TV si decide ogni volta.
- **`keepawake.py`**: blocco della sospensione chiesto a logind (`sleep` per un file, `sleep:idle` per lo schermo). Visto con `systemd-inhibit --list`, compare e sparisce [V].
- **La sessione**: stop e nuove scelte sorpassano ciò che è ancora in corso (generazioni); i messaggi sono tradotti dove nascono; ciò che ferma un flusso arriva nello stato.

Provato sulla TV vera, con la finestra vera su un display Broadway, la ricerca Avahi vera e clip fatte al momento [V]:
- foto: 4,3 s la prima (avvio del player), avanti e indietro; stop che richiude il player (lo stato Cast della TV non elenca più app);
- MP4 diretto: 4,3 s, pausa, ripresa, salto a 20 s;
- da un film all'altro senza stop: 0,3 s;
- H.264 + AC-3 con audio convertito, salto a 15 s;
- MPEG-4 + MP3 convertito tutto, salto a 20 s;
- H.264 a 10 bit convertito col codificatore software;
- stop durante il caricamento e foto subito dopo: 1,2 s;
- finestra chiusa con qualcosa in onda: il player si richiude.

Scoperto [V]:
- **Il codificatore della GPU può piantarsi.** H.264 a 10 bit o 4:4:4, che la TV rifiuta: il decoder software li dà in formati che `vapostproc` non negozia; convertiti in NV12 e passati a `vapostproc` e `vah264enc`, un errore di pagina nel VCN (`ring vcn_unified_0 timeout`, reset del solo anello, processo abortito, niri illeso; Radeon 760M, Mesa 26.2.6, kernel 7.3.0). Questi formati ora restano lontani dalla GPU (`transcode.gpu_takes`): codificatore software, o un messaggio se non c'è. La causa precisa nel VCN non è stata cercata: non si rifanno prove che fanno reagire il driver.
- **Il Default Media Receiver si chiude 0,3 s dopo che l'ultimo mittente se n'è andato**, anche se un altro si è appena collegato: il `LOAD` del nuovo resta senza risposta. Per questo una connessione sorpassata durante il `LAUNCH` (che la prima volta dura 4 s) passa al file nuovo invece di chiudersi.
- **`close()` su un socket non sveglia un lettore in attesa**: quel lettore continua sul numero di socket, e se il numero viene riusato legge i record TLS della connessione nuova (`RECORD_LAYER_FAILURE` sulla TV). Ora `shutdown` prima di `close`, e `Receiver.close` aspetta il lettore.
- **Leggere e scrivere insieme sullo stesso oggetto TLS** ha fallito una volta su circa sedici giri sotto carico ("A failure in the SSL library"), mai sulla TV: ora ogni chiamata sta sotto un lock, su un socket non bloccante, con l'attesa in `select`. Dopo la correzione 37 giri senza errori, che su un guasto così raro è un indizio più che una prova [I].

Il mirroring dentro l'app funziona sulla TV, schermo e singola finestra, confermato a mano il 03/11/2026 [V]: l'immagine arriva col ritardo atteso, una finestra resta proporzionata anche ridimensionandola, il suono arriva alla TV.

Scoperto col mirroring [V]:
- **Finestre.** La misura data dal portale su niri non serve: usciva 0×0 e la catena non si costruiva. `add-borders` di `vapostproc` sulla Radeon 760M non fa nulla: un 800×800 esce stirato a 16:9. Il pad di `vacompositor` posiziona e ridimensiona l'immagine.
- **Ridimensionare.** `vapostproc` continua a produrre la prima misura, con l'immagine nuova stirata dentro, quindi il compositore non vede cambiare niente: la misura si legge all'ingresso di `vapostproc`, e scalare quel riquadro alla forma nuova rimette le proporzioni. Il blocco di 10 secondi visto una volta, dopo il primo ridimensionamento, non è ricomparso; la causa non è stata trovata [I].
- **Trovare la TV.** Per qualche minuto Avahi ha avuto la TV solo su IPv6 e andava in timeout sull'indirizzo IPv4, mentre la TV rispondeva a una domanda mDNS diretta con tutto in un pacchetto. Causa ignota, poi si è ripreso da solo. Ora si cerca su entrambi i protocolli risolvendo sempre in IPv4, e dopo 2 secondi senza TV (e a ogni "Cerca di nuovo") si chiede anche direttamente.

Resta: MPRIS, `Cast1` e il file di stato con il documento del contratto, il manifest Flatpak (Avahi, logind, pulseaudio; `vainfo`, `pw-dump` o il device provider e un codificatore software nel runtime), la prima release.

## Appendice G. L'icona

L'icona definitiva è entrata con il ramo `feat/family-icon`: una lanterna con la sua fiamma su disco scuro; il negativo simbolico, senza fiamma; il disegno per 16-24 px in `build-aux/windows/lanterna-small.svg`, che `tools/make_ico.py` usa da solo. Il modello è `docs/lanterna-icona.svg` (Inkscape, una pagina) e non va installato.

Dal 22/11/2026 l'icona è nello stile di famiglia: la stessa lanterna, con le stesse misure, come un solido unico a spigoli vivi (il cerchio pieno in alto) in un colore solo, la sfumatura avorio del modello; la fiamma non simmetrica, con una linguetta a sinistra, piegata in quattro facce che si incontrano in un punto, dall'oro all'arancio (`#F2B94C`, `#EAA843`, `#E29A3E`, `#D4822F`), sopra la fiamma intera in un colore solo perché tra le facce non si veda il fondo; sul disco con lo spessore del modello (faccia di raggio 232 a 248, la sua copia 16 px sotto). Il disco è grigio caldo, più chiaro del nero del modello perché si stacchi dalle dock scure; i suoi toni vengono dai passi dell'avorio in OKLab (regola del modello: faccia `#48443e` → `#38342f`, fianco `#312a22` ai lati e `#1f1911` al centro). `tools/make_ico.py` è quello del modello: sulla barra delle applicazioni l'icona arriva al bordo.

Se il disegno cambia: si modifica il modello, si esporta la pagina come Plain SVG al posto dell'icona, con `<svg` entro i primi 256 byte, e si rilancia `tools/make_icons.py`.

Da verificare con il manifest Flatpak e il primo rilascio, mai provati [I]: l'icona nel launcher dall'app installata; l'icona nella barra del titolo su Windows, a 100% e a 125%.

## Appendice H. 03/11/2026: il Flatpak

Il manifest ha i permessi veri, ciascuno col suo motivo: rete, Avahi e logind sul bus di sistema, PulseAudio per il suono dello schermo, la home in sola lettura. Lo schermo non ha bisogno di permessi: chiede il portale.

Provato nella sandbox, installata in locale con `org.flatpak.Builder` [V]:
- runtime GNOME 50: Python 3.13, GStreamer 1.26 (sull'host 1.28), `vah264enc`, `x264enc`, i decoder AC-3 e DTS, `pulsesrc`, `pipewiresrc`; manca `openh264enc`, non serve;
- la suite intera nella sandbox: 185 test, 5 saltati;
- la TV trovata sia da Avahi sia con la domanda diretta;
- il blocco della sospensione: `systemd-inhibit --list` lo mostra (`Lanterna`, `sleep:idle`);
- `.desktop` e icone esportati in `~/.local/share/flatpak/exports`.

Da provare a mano: l'icona nel launcher e il mirroring dall'app installata. Nella sandbox `pw-dump` non vede PipeWire, quindi il numero del nodo deve venire dal device provider attraverso il descrittore del portale, oppure si lascia scegliere a `pipewiresrc` [I].

Provato a mano dall'app installata [V, 03/11/2026]: icona nel launcher, mirroring dell'intero schermo e di una finestra, ridimensionamento. Il numero del nodo nella sandbox si legge dal device provider, attraverso il descrittore del portale.

Trovato a mano e corretto:
- **Il salto in un flusso dal vivo faceva tornare la finestra alla prima pagina**: saltare è un flusso nuovo, e la TV annuncia la fine del vecchio durante il caricamento o subito dopo. Ora un annuncio che arriva mentre si carica, o con il numero di una sessione che non è più in corso, non è la fine di ciò che è in onda. Le prove automatiche guardavano solo lo stato finale: ora i test guardano anche quelli intermedi. Dopo la correzione non è ancora stato riprovato sulla TV [I].
- **Il suono doppio nel mirroring**: il computer tace mentre il suono va alla TV (`localsound.py`, preferenza), perché il monitor di PipeWire si prende prima del muto: un tono su un'uscita muta legge -26 dB [V]. Solo un'uscita mutata da noi viene riaperta, e il suo nome resta nelle impostazioni finché non è riaperta, così un crash non lascia il computer muto.

Limite che resta: un flusso dal vivo (film con audio convertito o tutto convertito) per la TV è una diretta senza durata. Il telecomando salta a scatti di dieci secondi dentro ciò che la TV ha già ricevuto; per andare ovunque nel film si usa la barra di Lanterna.

## Appendice I. 04/11/2026: il salto nei film veri

Dopo la correzione dell'appendice H, a mano non cambiava nulla: la correzione era vera ma non era la causa principale, e il muto non funzionava affatto nel Flatpak. Provato con un film vero (6 GB, H.264 1080p, due tracce AC-3) invece delle clip fatte al momento, sulla TV e poi a ripetizione senza TV: **un salto su dieci** falliva o si bloccava. Trascinando la barra ne partono diversi, quindi capitava quasi sempre. Quando fallisce, lo stato di errore riporta alla prima pagina: è ciò che si vedeva.

Le cause, ciascuna riprodotta [V]:
- **Il salto mandato troppo presto a `matroskademux`.** `parsebin` mostra le tracce mentre il demuxer legge ancora le intestazioni: il salto resta in sospeso e viene eseguito a metà della lettura del primo cluster, così i byte dell'intestazione di quel cluster si saltano anche nel punto nuovo. FFmpeg scrive le intestazioni lunghe quanto serve (6 o 7 byte secondo la dimensione): dove è diversa dalla prima il demuxer va avanti sfasato di un byte e si ferma, con "Could not demultiplex stream" o "Stream doesn't contain enough data", oppure esce un flusso tronco senza nessun errore. Dipende dal punto, non dai tempi. Le clip di prova non lo mostravano perché i loro cluster erano tutti della stessa taglia. Ora il salto parte dopo il primo dato.
- **Lo stallo con due tracce audio.** Le tracce scartate finivano in un `fakesink`, che tiene il primo buffer finché la pipeline non è in riproduzione, qualunque sia il suo `async`. La pipeline va in riproduzione quando l'`appsink` ha qualcosa, che richiede il suono, che passa dall'unico thread del demuxer, fermo in quel `fakesink`. Senza errori e per sempre. Ora una sonda butta i dati, senza elementi.
- **Il muto rifiutato nel Flatpak.** WirePlumber dà ai client Flatpak solo lettura ed esecuzione: `set-sink-mute` risponde "Access denied". Da sorgente non si vede. Chi si dichiara mixer (`media.category=Manager`, regola in `find-flatpak-access.lua`) può: lo dichiara solo il `pactl` di `localsound.py`.
- **La barra disattivata durante il caricamento** avrebbe interrotto il trascinamento, e una pausa di un quarto di secondo faceva già partire un salto. Ora con il tasto premuto non parte nulla finché non si lascia; tastiera e rotellina restano con l'attesa.

In più: le sonde ora si tolgono da sole al FLUSH_STOP, così i primi dati del punto nuovo non possono andare persi; un flusso che non produce nulla per 20 secondi diventa un errore detto, invece di un'attesa senza fine.

Provato:
- `transcode` da solo, film vero: da 12 fallimenti su 120 a 0 su 300, e 60 su 60 dall'inizio [V]. `tests/data/two-cluster-sizes-two-sound-tracks.mkv` (139 KB, il comando è nel test) mostra sul codice vecchio tutti i sintomi, a colpo sicuro: flusso tronco, stallo, i due errori [V].
- La finestra vera sul film vero e sulla TV, con eventi di mouse veri: 11 salti, trascinamenti con esitazioni, un secondo trascinamento mentre la TV caricava il primo. Un salto solo per ogni rilascio, sempre sulla pagina del film, la TV riparte in circa 0,4 s [V]. Questa prova ha trovato un errore che i test non vedevano: l'argomento del segnale `event` di `EventControllerLegacy` arriva in Python come `None`.

Come si fa quella prova, senza schermo e senza toccare la sessione dell'utente: `gtk4-broadwayd`, Chromium senza interfaccia sulla sua pagina e il protocollo DevTools per chiamare `sendInput` della pagina con le costanti `BROADWAY_EVENT_*`. La pagina non disegna, perché `requestAnimationFrame` non scatta in quella modalità, ma GTK riceve gli eventi veri. Lo schermo della TV si fotografa con `tools/tv.py --screenshot` di cleanyourtv: per il ricevitore Cast l'immagine esce vera.

Sulla TV, con le foto dello schermo: il salto arriva nella scena giusta e l'immagine è pulita già dopo un secondo; alla fine vera del film la finestra torna libera, a 8710 s su 8710 [V]. Nel Flatpak installato: 300 avvii su 300 in AUDIO e 25 su 25 in TRANSCODE con il codificatore software, e il muto vero sull'uscita del computer, che poi torna. Il mirroring con il muto, dall'inizio alla fine, resta da provare a mano perché il selettore chiede ogni volta [I].

Limite rimasto: il flusso può partire solo da un'immagine chiave, quindi la barra è avanti rispetto all'immagine di quanto manca a quella precedente: in questo film in media 3 s, al massimo 10 (a 3000 s parte da 2994,6) [V]. Si potrebbe correggere leggendo il tempo del primo buffer.

Nota per il CI: il lavoro Linux non installa GStreamer, quindi `test_transcode` lì viene saltato, compresa la prova qui sopra. Da sistemare quando il repository sarà su GitHub.


## Appendice J. 22/11/2026: la pubblicazione, 0.2.0

Il repository diventa pubblico su GitHub (`daniel-g-carrasco/lanterna`) con la 0.2.0, prima versione pubblica (la 0.1.0 era il modello, mai pubblicata). Su GitHub va una storia sola: ogni pubblicazione è un commit sopra il `main` pubblico, la storia di lavoro resta qui.

Prima della pubblicazione:
- tolti i dati di casa dalle prove e dal diario: il nome della TV, gli indirizzi della rete, l'identificativo del Chromecast (al loro posto valori inventati: "Living Room TV", `192.168.1.x`);
- `CLAUDE.md` non è più nel repository (resta in locale, in `.gitignore`), come nelle altre app;
- `tools/check_public.py` del modello, con `.public-emails` per l'indirizzo dei commit;
- chiave GPG dedicata al repository Flatpak, RSA 4096 "Lanterna Flatpak repository", impronta `8CC344A40BD6693E229557FDFBC115ECDDE3477A`, privata in `~/.config/lanterna-flatpak/private-key.asc` (600) e nel segreto `FLATPAK_GPG_PRIVATE_KEY`; la chiave minisign delle release è quella condivisa con P7M Extractor, PDF Cropper e Cartiglio.

README rifatto sul modello di quello di Spola, con due GIF d'uso e una piccola galleria in chiaro e in scuro. Le immagini sono la finestra vera, guidata dalle prove con la TV finta (`tests/_fake_tv.py`) e foto inventate, su un display Broadway a parte, mai nella sessione: `tools/screenshots/` (`run.sh`, `demo.py`, `compose.py`; la TV disegnata accanto alla finestra mostra la foto che Lanterna le ha mandato). Due trappole [V]: senza un browser collegato a Broadway le animazioni non avanzano (un dialogo resta trasparente, uno `Gtk.Stack` a metà dissolvenza), quindi `gtk-enable-animations` spento; e il CSS utente di GTK (quello di Noctalia) cambia i colori, quindi `XDG_CONFIG_HOME` vuoto. L'accento resta quello del sistema (giallo sul portatile).
