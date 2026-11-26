# Grooves: what measurements of real recordings tell us about musical feel

A sequencer puts every note exactly on its grid. Musicians don't, and in most music they aren't sloppy about it:
the small offsets from the grid are part of the style. Samba, Viennese waltz, a jazz ride cymbal and the drum
break on a soul record each have their own pattern, repeated beat after beat and recognised by every
listener raised on that music.

This document reviews studies that **measured** these patterns in recordings: onsets detected from audio and
compared with an even grid. It explains how the
[groove presets](src/ostinato/music/grooves.py) in Ostinato were derived from those measurements. Only studies with
a measurement behind them are used. Where a figure could not be checked against the paper itself, this is said.

## How a groove is measured

Two ways of describing the same thing appear in the literature:

- **Offsets from the grid ("microtiming").** Each note's onset is compared with its place on an even subdivision of
  the beat, and the difference is given in milliseconds or as a fraction of the beat. Samba research mostly works
  this way.
- **Uneven subdivision ("non-isochronous" beats).** The beat is divided into parts of unequal length, given as
  proportions (27 : 33 : 40) or, for two parts, as a *swing ratio* (long : short; 2 : 1 is the "triplet" swing).
  Jazz and West African research mostly works this way, and some authors argue it describes the music better:
  the uneven subdivision *is* the metre, not a deviation from it ([Polak 2010](#ref-polak2010)).

Ostinato stores a groove the first way: per step, an offset in 1/64 of a step (about 1.6 %), repeated over the
pattern. It writes it into each step's own timing offset, which is what the FM-1 calls a step's *nudge* (also in
1/64 of a step), so every device plays the same groove. Both kinds of finding convert to it:

- a **swing ratio r** puts the off-beat at r / (1 + r) of the pair, so it moves by **(2r / (1 + r) − 1)** of a step;
- **proportions** A : B : C … give onsets 0, A, A + B, …; their distance from the even positions, divided by the
  step, is the offset.

Because a groove is stored in steps, not milliseconds, it scales with the tempo. That matches what the studies find
for samba and jembe, and it is a simplification for jazz, where the swing ratio itself changes with the tempo (below).

## Samba: the early third and fourth sixteenth

Samba is played on a fast grid of four sixteenths per beat, and percussionists fill all of them. Since Gouyon
analysed 49 samba-de-roda excerpts from commercial CDs ([Gouyon 2007](#ref-gouyon2007)), study after study has found
the same thing: **the third and fourth sixteenths of each beat arrive early.**

The largest study, by [Naveda, Gouyon, Guedes and Leman (2011)](#ref-naveda2011), analysed **106 samba recordings**
(mean tempo 103 BPM). The recordings were filtered into low, mid and high frequency bands, roughly the surdo, the
middle instruments and the shakers and tamborins. In every band:

| Sixteenth | Mean offset (beats), low / mid / high band | At 103 BPM |
|---|---|---|
| 3rd | −0.026 / −0.031 / −0.032 | 16 / 18 / 19 ms early |
| 4th | −0.028 / −0.018 / −0.027 | 16 / 11 / 16 ms early |

They also found two smaller effects. In the low band (the surdo) the first sixteenth is slightly *late*. And the
timing speeds up and slows down slightly over two- and four-beat phrases.

[Haugen and Godøy (2014)](#ref-haugen2014) recorded a pandeiro player and a samba dancer with motion capture. In the
sound, the fourth sixteenth was clearly the **longest** of the four, and the first three did not differ from each
other. The percussionist's heel taps followed the beat and the fourth sixteenth. The dancer's steps followed the first
three, and seemed to copy the durations of the previous bar. The authors conclude that this timing is not a
deviation from an even pulse the musicians "really" hear. It is how samba's beat is shaped, in the music and in the
body.

**Preset "Samba"** (1/16 steps): `0, 0, −8, −6` /64, which is 0, 0, −12.5 %, −9.4 % of a sixteenth. These are the
means of the three bands in Naveda et al. The second sixteenth stays on the grid, because no study found it late.

## Jazz swing: a ratio that depends on the tempo

Swing is the long–short division of each beat's eighth notes. The textbook value is the triplet, 2 : 1. Measurements
show that real swing ratios depend on the instrument and above all on the tempo.

- [Friberg and Sundström (2002)](#ref-friberg2002) measured recordings by the Miles Davis Quintet, Wynton Marsalis
  and the Keith Jarrett Trio. At medium tempi the **drummers' ride cymbal swung at more than 2 : 1, the soloists at
  less**. The soloists were **late on the downbeats and together with the drummer on the upbeats**, which flattens
  their swing. The ratio falls as the tempo rises: high at slow tempi, approaching even eighths at very fast ones.
- [Honing and de Haas (2008)](#ref-honing2008) confirmed with expert drummers that the swing ratio is adapted to the
  tempo rather than kept constant.
- [Datseris et al. (2019)](#ref-datseris2019) recorded a professional jazz pianist on twelve standards. The average
  swing ratio per piece ran from **1.9–2.1 at 130–140 BPM** down to **1.6–1.7 at 160–190 BPM**.
- Their listening test is a warning against overdoing it. Versions with the pianist's timing *quantized* to his average
  swing ratio were rated as swinging slightly *more* than the originals, and doubled deviations as the least. The
  systematic ratio matters; random wobble does not. A follow-up ([Nelias et al. 2022](#ref-nelias2022)) analysed
  456 jazz performances and found the soloists' downbeats typically **delayed** against the rhythm section, with the
  off-beats together. In a listening test, musicians rated versions with delayed downbeats and synchronised off-beats
  as swinging most.
- [Corcoran and Frieler (2021)](#ref-corcoran2021) analysed the swing eighths of hundreds of solos in the Weimar Jazz
  Database. Their title, "Playing It Straight", summarises their conclusion that soloists swing much less than the
  textbook triplet.

**Presets "Jazz swing (medium tempo)"** `0, +21` /64 (+33 %) and **"Jazz swing (fast tempo)"** `0, +16` /64 (+25 %),
both on 1/8 steps. These are swing ratios of 2 : 1 and about 1.65 : 1. On sixteenth steps, swing beyond about 1.6 : 1
would need more than half a step, so these presets apply to tracks with eighth-note steps.

Ostinato's groove applies one feel to the whole ensemble. The ride-against-soloist difference that Friberg and
Sundström describe would need a groove per track: the presets give the ensemble's average swing.

## Sixteenth swing in funk and soul drum breaks

Hip-hop is built on drum breaks sampled from funk and soul records. [Frane (2017)](#ref-frane2017) measured
**30 classic breaks** from that canon. Most of them swing their sixteenths, delaying every second one, but subtly:
the **median ratio is 1.2 : 1**, far from a triplet. Unlike in jazz, the amount of swing did not follow the tempo
(within the fairly narrow range of tempi in the corpus). The snare on beat 2 was often late, the one on beat 4 was
not. Frane also points out that swing is only heard where there are notes to swing: he calls the share of swung
positions that hold a note the *swing density*.

**Preset "Breakbeat sixteenth swing"** (1/16 steps): `0, +6` /64, which is +9.4 %, the median ratio 1.2 : 1. This is
the closest the measured literature comes to a "funk" preset. Danielsen's analysis of James Brown and Parliament
([Danielsen 2006](#ref-danielsen2006)) is the classic study of funk microrhythm, but it does not give a single
pattern that could be used as a preset.

## The Viennese waltz: the second beat comes early

Players of Viennese waltzes are famous for not playing three even beats. The second beat comes early and the bar
has a lilt that follows the dance's long first step. [Bengtsson and Gabrielsson (1977)](#ref-bengtsson1977) measured
this in early timing studies.

[Yang (2022)](#ref-yang2022) compared **34 recordings of *The Blue Danube***. The Viennese orchestras, the Vienna
Philharmonic above all, play the three beats **short–long–medium, about 1 : 1.4 : 1.3**. The orchestras from
elsewhere play them nearly even. The style is regional, not a general property of waltzes.

**Preset "Viennese waltz"** (1/4 steps): `0, −11, −3` /64: the second beat early by about 17 % of a beat, the third by
5 %. On 1/8 steps the lag is sampled between the beats: `0, −12, −22, −14, −6, −4`.

## Jembe music from Mali: uneven beats as metre

Rainer Polak measured jembe ensembles in Bamako over several years. In many pieces the beat is divided into
**unequal parts that keep their proportions across the whole performance**, while the tempo accelerates from about
135 to over 200 BPM ([Polak 2010](#ref-polak2010)).

- **Manjanin** (three pulses per beat): short–medium–long, a mean of **27 : 33 : 40** % of the beat in the lead
  jembe's fast passages at about 190 BPM. The deviation from beat to beat is only about 2 %. Of four lead drummers
  compared, three played short–medium–long and one leaned towards short–long–long. The accompanying second jembe plays its own 60 : 40.
- **Woloso** in the binary version (two pulses): long–short, **58 : 42** on average (53 : 47 to 58 : 42).
- **Sunun** (four pulses): short–medium–long–medium. This review could only confirm approximate ranges for it, so it
  is not a preset.

[Polak, London and Jacoby (2016)](#ref-polak2016) took about **43,000 onsets** from fifteen recordings of three
pieces. The ensembles were just as precise and stable in the uneven pieces as in the even one. To the players,
these beats are not "swung" versions of an even beat. The uneven beat is the reference itself.

**Presets "Jembe: Manjanin"** (triplet steps, 8T): `0, −12, −13` /64. **"Jembe: Woloso"** (1/8 steps): `0, +10` /64
(+16 %, the 58 : 42 pair).

## Laid back and pushed: timing between instruments

"Laid back" and "pushed" mean an instrument played slightly behind or ahead of the others, not a shape within the
beat. [Câmara, Nymoen, Lartillot and Danielsen (2020)](#ref-camara2020) asked **22 professional drummers** to play
the same back-beat laid-back, on the beat and pushed, against a metronome and against a band track. The onsets
followed the instruction for every drum. The band track drew out stronger timing than the metronome. And the
drummers changed their sound as well as their timing: laid-back snare strokes were longer, and intensity differed
between the styles. A sister study ([Câmara et al. 2020b](#ref-camara2020b)) found the same with 21 guitarists and 21
bassists. The papers describe these offsets in the range of tens of milliseconds.

There is no preset for this yet. It would be a constant offset on one track's groove, for example the snare or the
bass behind everything else, and the exact figures are in tables this review could not read.

## Do listeners notice?

[Kilchenmann and Senn (2015)](#ref-kilchenmann2015) played funk and swing clips with altered microtiming to
**160 listeners** and measured their body movement. The effect was small to moderate, and it showed only in the
**music experts**. Together with the jazz study above, this suggests that a groove works through its **systematic
pattern**, not through random deviation. That is exactly what a preset provides, and why Ostinato's grooves are
stored as a fixed pattern rather than as humanising noise.

## The presets at a glance

| Preset | Steps | Values (1/64 of a step) | Measured in |
|---|---|---|---|
| Samba | 1/16 | 0, 0, −8, −6 | 106 samba recordings ([Naveda et al. 2011](#ref-naveda2011)) |
| Jazz swing (medium tempo) | 1/8 | 0, +21 | pianist, 130–140 BPM ([Datseris et al. 2019](#ref-datseris2019)) |
| Jazz swing (fast tempo) | 1/8 | 0, +16 | pianist, 160–190 BPM ([Datseris et al. 2019](#ref-datseris2019)) |
| Breakbeat sixteenth swing | 1/16 | 0, +6 | 30 drum breaks ([Frane 2017](#ref-frane2017)) |
| Viennese waltz | 1/4 (or 1/8) | 0, −11, −3 | 34 *Blue Danube* recordings ([Yang 2022](#ref-yang2022)) |
| Jembe: Manjanin | 8T | 0, −12, −13 | jembe ensembles, Bamako ([Polak 2010](#ref-polak2010)) |
| Jembe: Woloso | 1/8 | 0, +10 | jembe ensembles, Bamako ([Polak 2010](#ref-polak2010)) |

**Finer grids.** A preset also goes onto a grid two, four or eight times finer: its lag is sampled at the extra
steps. It never goes onto a grid of the other kind (triplet feels onto binary steps, or the reverse), and never onto
a step length that would need more than half a step. Every value is rounded to 1/64 of a step: about 2 ms for a
sixteenth at 120 BPM.

## Open questions

- **Tempo dependence.** Jazz swing flattens as the tempo rises, so a preset in steps is right for one range of tempi.
  The two jazz presets cover medium and fast.
- **One feel per ensemble.** Several studies find different timing per instrument: the ride against the soloist in
  jazz, the surdo's late first sixteenth in samba, the jembe parts' different proportions. Ostinato's single groove
  takes the ensemble's common pattern.
- **Missing figures.** Laid-back and pushed offsets ([Câmara et al. 2020](#ref-camara2020)), the samba pandeiro
  durations ([Haugen & Godøy 2014](#ref-haugen2014)) and jembe Sunun ([Polak 2010](#ref-polak2010)) would make further
  presets once their tables are checked.

## References

- <a id="ref-bengtsson1977"></a>Bengtsson, I., & Gabrielsson, A. (1977). Rhythm research in Uppsala. In *Music,
  Room, Acoustics* (Publication No. 17, pp. 19–56). Royal Swedish Academy of Music, Stockholm. Cited after
  [Yang 2022](#ref-yang2022); not read for this review.
- <a id="ref-camara2020"></a>Câmara, G. S., Nymoen, K., Lartillot, O., & Danielsen, A. (2020). Timing is
  everything… or is it? Effects of instructed timing style, reference and pattern on drum kit sound in groove-based
  performance. *Music Perception, 38*(1), 1–26. https://doi.org/10.1525/mp.2020.38.1.1
- <a id="ref-camara2020b"></a>Câmara, G. S., Nymoen, K., Lartillot, O., & Danielsen, A. (2020). Effects of
  instructed timing on electric guitar and bass sound in groove performance. *Journal of the Acoustical Society of
  America, 147*(2), 1028–1041. https://doi.org/10.1121/10.0000724
- <a id="ref-corcoran2021"></a>Corcoran, C., & Frieler, K. (2021). Playing it straight: Analyzing jazz soloists'
  swing eighth-note distributions with the Weimar Jazz Database. *Music Perception, 38*(4), 372–385.
  https://doi.org/10.1525/mp.2021.38.4.372
- <a id="ref-danielsen2006"></a>Danielsen, A. (2006). *Presence and Pleasure: The Funk Grooves of James Brown and
  Parliament*. Wesleyan University Press.
- <a id="ref-datseris2019"></a>Datseris, G., Ziereis, A., Albrecht, T., Hagmayer, Y., Priesemann, V., & Geisel, T.
  (2019). Microtiming deviations and swing feel in jazz. *Scientific Reports, 9*, 19824.
  https://doi.org/10.1038/s41598-019-55981-3 (preprint: https://arxiv.org/abs/1904.03442)
- <a id="ref-frane2017"></a>Frane, A. V. (2017). Swing rhythm in classic drum breaks from hip-hop's breakbeat
  canon. *Music Perception, 34*(3), 291ff. https://doi.org/10.1525/mp.2017.34.3.291
- <a id="ref-friberg2002"></a>Friberg, A., & Sundström, A. (2002). Swing ratios and ensemble timing in jazz
  performance: Evidence for a common rhythmic pattern. *Music Perception, 19*(3), 333–349.
  Summary with figures: https://www.speech.kth.se/music/performance/Texts/ensemble_swing.htm
- <a id="ref-gouyon2007"></a>Gouyon, F. (2007). Microtiming in "Samba de Roda": Preliminary experiments with
  polyphonic audio. *Brazilian Symposium on Computer Music (SBCM 2007)*.
  https://compmus.ime.usp.br/sbcm/2007/papers/sbcm-2007-17.pdf
- <a id="ref-haugen2014"></a>Haugen, M. R., & Godøy, R. I. (2014). Rhythmical structures in music and body movement
  in samba performance. *Proceedings of the 13th International Conference on Music Perception and Cognition (ICMPC
  13)*, Seoul.
- <a id="ref-honing2008"></a>Honing, H., & de Haas, W. B. (2008). Swing once more: Relating timing and tempo in
  expert jazz drumming. *Music Perception, 25*(5), 471–476. https://doi.org/10.1525/mp.2008.25.5.471
- <a id="ref-kilchenmann2015"></a>Kilchenmann, L., & Senn, O. (2015). Microtiming in swing and funk affects the body
  movement behavior of music expert listeners. *Frontiers in Psychology, 6*, 1232.
  https://doi.org/10.3389/fpsyg.2015.01232
- <a id="ref-naveda2011"></a>Naveda, L., Gouyon, F., Guedes, C., & Leman, M. (2011). Microtiming patterns and
  interactions with musical properties in samba music. *Journal of New Music Research, 40*(3), 225–238.
- <a id="ref-nelias2022"></a>Nelias, C., Sturm, E. M., Albrecht, T., Hagmayer, Y., & Geisel, T. (2022). Downbeat
  delays are a key component of swing in jazz. *Communications Physics, 5*, 237.
  https://doi.org/10.1038/s42005-022-00995-z
- <a id="ref-polak2010"></a>Polak, R. (2010). Rhythmic feel as meter: Non-isochronous beat subdivision in jembe
  music from Mali. *Music Theory Online, 16*(4).
  https://mtosmt.org/issues/mto.10.16.4/mto.10.16.4.polak.html
- <a id="ref-polak2016"></a>Polak, R., London, J., & Jacoby, N. (2016). Both isochronous and non-isochronous
  metrical subdivision afford precise and stable ensemble entrainment: A corpus study of Malian jembe drumming.
  *Frontiers in Neuroscience, 10*, 285. https://doi.org/10.3389/fnins.2016.00285
- <a id="ref-yang2022"></a>Yang, J. (2022). Viennese style in Viennese waltzes: An empirical study of timing in the
  recordings of *The Blue Danube*. *Musicologica Austriaca*.
  https://musau.org/parts/neue-article-page/view/135
