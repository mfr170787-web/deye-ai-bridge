# Deye AI Bridge 0.8.0

Lokal pris- og solbaseret styring af husbatteriet via Home Assistant og EW11.

## Automatisk styring

- Knappen **Aktivér fuld automatisk styring** starter faktisk inverterstyring; planvisningen alene gør ikke.
- Planen prioriterer soloverskud, billige netpriser og batteriforbrug i dyre timer. Mellemdyre timer må kun bruge energi over den beregnede fremtidige husreserve.
- **Deaktivér og gendan** stopper planstyringen og skriver de oprindelige strømgrænser samt alle gemte TOU-tider, SOC-mål og ladeflag tilbage. Inverterens oprindelige drift kan derefter fortsætte; knappen slukker ikke inverteren.
- Alle seks TOU-perioder anvender den aktuelle handling. Tidsgrænserne er faste 00/04/08/12/16/20 under styring; den løbende plan bestemmer handlingen, også over midnat.
- Maksimalt 190 A samlet batteriladning med tilpasning til ladevinduet; afladning fortsat højst 90 A; lavere BMS-grænser samt de valgte effektgrænser har forrang. Planlægningsgrænser er højst 9000 W opladning og 4500 W afladning. Den tidligere standard på 4500 W opladning hæves én gang til 9000 W i 0.8.11; brugerdefinerede lavere grænser bevares. SOC-reserven er mindst 15 % og over inverterens Shutdown-niveau; Mål-SOC er højst 99 %.
- Reservehold stopper afladning; soloverskud kan fortsat oplade. Netladning bruger periodens beregnede SOC-mål. Ved mål-SOC begrænses ladning med hysterese.
- Der ændres ikke på BMS-grænser, Remote-mode, eksporttilstand, batterispændinger eller sikkerhedsindstillinger. Zero Export, Solar Sell fra og SOC/TOU skal allerede være korrekt indstillet.
- Når Tesla lader, eller dens effekt er ukendt, beskyttes den beregnede husreserve også mod batteriforbrug til bilen. Tesla startes og stoppes ikke af denne version.

## Før første aktivering

1. Opdatér tilføjelsen og kontrollér driftsplanens mål-SOC, reserve og effektgrænser. Gem driftsplanen.
2. Cloudlogger/Copilot skal være frakoblet. EW11 skal være tilsluttet. Afslut eventuel gendannelse fra tidligere ladetest.
3. Sæt `read_only: false` og `commissioning_enabled: true` i tilføjelsens indstillinger; gem og genstart.
4. Kontrollér begge BMS-visninger manuelt. Bekræft feltet i styringskortet, og aktivér under opsyn.
5. Følg både **bekræftet indstilling** og **faktisk måling**. De to statusser er adskilt.
6. Afprøv deaktivering og afvent mindst 60 sekunders efterkontrol. Hent analysepakken med styringslog og dagens/næste dags plan.

Aktivering kræver friske målinger, aktuelle Andel-priser inklusive transport/afgifter, solprognose, korrekt inverterur, nettilslutning og en gyldig plan for den aktuelle time. Ukendte morgendagspriser vises som manglende; der opfindes ingen prisplan.

## Fejl og genstart

Uventede aflæsninger, ændringer fra anden styring, forældede data eller ladeeffekt over softwaregrænsen 9500 W eller 165 A stopper styringen og udløser forsøg på gendannelse. Skriveintention og oprindelige værdier gemmes før hardwarekommandoer. Absolutte indstillinger kan gentages højst tre gange efter friske registerkontroller; ukendte værdier blokerer gentagelse.

Ved ændringer fra anden styring overskrives fremmede værdier ikke. Egne strømgrænser forsøges sat til nul, når gendannelse er blokeret, og der kræves manuel kontrol. Der skrives aldrig til en anden inverter efter ændring af forbindelsen.

Efter genstart forsøges gendannelse; automatisk drift genoptages først ved en ny manuel aktivering. Ved registreret netudfald forsøges gendannelse af inverterens oprindelige drifts-/backupgrænser.

**Dette er software, ikke en hardware-watchdog.** Hvis Pi, strømforsyning eller forbindelsen til EW11 svigter, kan sidste indstilling blive stående. En 25 A-sikring på inverterkredsen er ikke en præcis strømbegrænser. Softwarebaseret sikringsbeskyttelse er ikke implementeret eller en forudsætning i denne version. Bridge kan kun se den kommunikerende BMS; den anden pakke skal aflæses manuelt ved afprøvning.

Den nye automatiske styring er kontrolleret med simuleret Modbus og API-/modeltests. Første fulde forløb på den konkrete inverter/firmware skal stadig afprøves under opsyn.

## Dashboard og log

- Fast styringskort med aktivering, deaktivering, kontrolstatus, faktisk effekt og gemte originalværdier.
- Planvindue for alle timer i dag og i morgen, inklusive manglende priser og gemte tidligere planperioder.
- Pris, sol, forbrug, SOC-mål, reserve, begrundelse, målte værdier og tilbagelæste kommandoer.
- Scanner, registerkontrol, tidslog, komplette snapshots, energiflow og analyseeksport er bevaret.
- Manuelle observationer og gamle testjournaler er bevaret; de gamle ladetest kan ikke længere startes.
- Historik gemmes lokalt. Aktiveringsjournaler med inverterens adresse vises/eksporteres uden endpoint.

## Installation og kontrol

Opdatér fra det eksisterende repository i Home Assistant. Ved lokal installation bruges hele mappen `deye_ai_bridge`, inklusive `automatic_control.py`.

Kør softwarekontroller med:

```sh
python -m unittest discover -s deye_ai_bridge/tests -v
```

### 0.8.12
Telefonbesked om automatisk styring sendes kun ved endeligt stop, når automatisk genoptagelse ikke længere forsøges. Gendannelse, efterkontrol og automatiske genforsøg er tavse. Ved fortsat utilgængelig Modbus afventes forbindelsen uden gentagne beskeder. 150 A-ladegrænsen fra 0.8.11 bevares.

### 0.8.13
Husplanen beregner nu samlet importpris på tværs af alle sammenhængende kendte pristimer (op til 48 timer), inklusive ladetab, SOC- og effektgrænser. Billig nat kan dække både morgen og dyr aften i stedet for en dyrere middagsladning. Sol krediteres fortsat 70 %, husforbruget tillægges den valgte procentvise sikkerhedsmargin. Energi fordeles i trin på 0,25 kWh; dette er en prognosebaseret beregning, ikke en garanti for faktisk forbrug. Ukendte prisperioder anvendes ikke som ladevinduer. Modellen begrænses også af de aktuelle BMS-strømgrænser og 150 A ladning / 90 A afladning. Scanner, logger, gendannelse og tavse automatiske genforsøg bevares.

### 0.8.14
Retter en overgangsfejl: automatisk genoptagelse blev kort markeret som opgivet, mens aktivering endnu læste og kontrollerede inverteren. Fejlbeskeder kunne derfor blive sendt ved vellykket genstart. Genoptagelse forbliver nu markeret som igangværende indtil aktiveringen er journalført. Beskedhistorikken medtages i analysepakken for at kunne kontrollere faktisk afsendelse.

### 0.8.15
- Modbus-læsninger får op til tre forsøg med kort stigende pause og ny TCP-forbindelse. Protokolfejl og skrivekald gentages ikke af denne læsefunktion.
- Vellykkede genoptagelser nulstiller genforsøgstælleren. Tre mislykkede genoptagelser medfører 15 minutters pause, derefter mindst tre gyldige kontroller over 30 sekunder før ny aktivering. Manuelt stop, procesgenstart og fremmede registerværdier giver ikke automatisk genaktivering. Mislykket gendannelse kan forsøges igen i adskilte forløb efter 15 minutter; fremmede værdier blokerer stadig.
- Telefonen adviseres først ved opgivet genforsøgsforløb eller fejl, der kræver indgriben; pausebeskeden angiver at automatisk kontrol prøves igen.
- Loft for samlet ladning er 190 A; afladning forbliver højst 90 A. Lavere modtaget BMS-grænse respekteres. Ingen fordobling af den ene BMS-værdi og ingen automatisk forhøjelse til 240 A.
- Ladning beregnes efter manglende SOC, kapacitet, ladetab og resterende tid i sammenhængende planlagte netladetimer, med 10 % tidsmargin og strømtrin på 5 A. Et langt ladevindue giver lavere strøm; loftet anvendes kun ved behov. Ændret prognose eller målt SOC medfører ny beregning.
- Rene strømjusteringer afbryder ikke længere netladningsflagene. Små variationer dæmpes. Gamle 150A-profiler med 8000/9000 W hæves én gang til 11000 W, så 190 A kan udnyttes; andre brugergrænser bevares. Det er et effektloft, ikke et konstant effektkrav.
