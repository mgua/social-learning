# Buongiorno!

Questo è **social-learning**, un semplice e compatto document manager per utenti singoli e piccoli gruppi che offre:

- interfaccia su qualsiasi browser
- integrazione con git
- funzionamento locale su windows/linux/macosx
- supporto ibrido viewing/editing/previewing
- supporto [markdown](https://commonmark.org)
- supporto gerarchico folders
- riferimenti a contenuti internet e intranet
- file-attachment qualsiasi
- Registrazioni audio/video dal browser
- Estremamente compatto: circa 35Kb
- no-dependencies: solo python3 e git


## Come si usa

* Forkare il progetto sulla vs. utenza github
* Clonare il repository forkato in una cartella locale del vostro sistema, utilizzando git o gh, come comandi simili ai seguenti (ma clonando dal vostro fork)

```bash
       git clone https://github.com/mgua/social-learning.git
       cd social-learning
       python3 app.py
```

Dopo l'aggiunta di contenuti tramite browser, salvate e committate a livello git. Poi git push sul vostro repo github

Condividete il vostro repo con i vostri amici, e potrete ediitare ciascuno autonomamente, poi sincronizzando su github. **Buona documentazione!**


## A cosa serve social-learning:

**social-learning** è ideale per:

- raccolta documentazione e appunti, compreso registrazioni lezioni
- istruzioni operative, procedure e processi 
- raccolta link e riferimenti commentati
- rendering grafici in formati markdown, come mermaid. Vedere [kroki](https://kroki.io)


**social-learning** si integra con github e consente accesso e community editing dei contenuti di un repository. Al momento la modifica di contenuti locali non richiede autenticazione, per non appesantire il codice che vuole essere tenuto il più compatto possibile. Di fatto l'identità dell'utente è definita dall'accesso al repository git.


# Esempi

- url link youtube con preview grafico: 
[Andrej Karpathy](https://www.youtube.com/watch?v=EWvNQjAaOHw)

- embedding audio registrato localmente:
[audio-1783858791253.weba](/content/_assets/20260712-141951-c1e722-audio-1783858791253.weba)

- Link a siti interattivi come projector.tensorflow.org: [Projector tensorflow](https://projector.tensorflow.org/)


____

# Welcome

This is **social-learning**, a compact local document manager.

## Try it out

- Create documents and folders with the buttons on the left.
- Write [markdown](https://commonmark.org) — headings, **bold**, *italic*,
  `code`, lists, quotes.
- Paste an image or a file directly into the editor.
- Paste a link — a YouTube link becomes a clickable thumbnail:
  https://youtu.be/dQw4w9WgXcQ
- Record audio (🎤) or video (🎥) from your camera/mic.

Everything you write is saved as files under `content/` so you can commit and
push it.
