# Deye AI Bridge 0.7.2

## Nyt i 0.7.2

- Løbende read-only logning af fjernstyring/watchdog (1100–1101), kontrolside/styringsform (1104–1105) og effekt/SOC-setpunkter (1109–1110).
- Små separate registerkald; læsefejl i nye områder gemmes uden at afbryde indsamlingen af kernemålinger. Der skrives fortsat intet til inverteren.
- Urregistre 62–64 sammenlignes med dansk lokal tid ved aflæsningen. Forskydning og ugyldigt ur vises i dashboardet; uret justeres ikke.
- Logning af batteriindstillinger, BMS-grænser og beregnede V × A-effektgrænser, samt 32-bit energitotaler fra 516–528. Effektgrænser er diagnostiske estimater og ændrer ikke planlægningen i denne version.
- Register 117 hedder korrekt LowBatt-SOC; Shutdown og Restart vises separat. Register 143 navngives maksimal salgseffekt.
- EV 259–260 logges som rå værdier med ikke-verificeret skala. 65535 i watchdog betyder deaktiveret; ukendte værdier i andre felter fortolkes ikke som gyldige grænser.
- Registerhistorik gemmes i samples.telemetry_json og seneste afkodning i modbus_telemetry.json i analysepakken. Eksisterende historik og brugerindstillinger bevares.
- Ny diagnosevisning i dashboardet for ur, batterigrænser og fjernstyring. Læsbare registre beviser ikke, at skrivning understøttes.

## Nyt i 0.7.1

- Husnoden viser dagens registrerede husforbrug, bilforbrug og total. Energi i perioder med usikre bildata vises særskilt som uafklaret i stedet for at blive tildelt huset.
- Under timeprisgrafen vises en liste med pris, købte kWh og beregnet beløb pr. time, inklusive dagssummer og markering af manglende data.
- Dagsværdier integreres fra minutmålingerne i dansk lokal tid; inverterens nulstillede dagstællere bruges ikke til denne fordeling.
- Netimport integreres separat fra eksport og fordeles på de rigtige timer, også over midnat og skift mellem sommer-/vintertid. Manglende måleperioder udfyldes ikke.
- Tesla-nodens dagsenergi bruger samme beregning som husets fordeling. Fordelingen er et estimat baseret på Tesla-effekt, ikke en separat Deye-ladermåler.
- Dagsfordeling og timeindkøb medtages i snapshot som daily_energy.json.

## Nyt i 0.7.0 — fortsat Shadow Mode

- Fælles prisvindue for beslutningslog og husplan; gamle timer påvirker ikke prisgrænserne.
- Foreløbig dynamisk husreserve i procent/kWh, forventet husbehov, næste genopladningsmulighed og muligt batteribidrag til Tesla.
- Reserven krediterer 70 % af solprognosen og anvender 15 % af batterikapaciteten som sikkerhedsmargin samt den eksisterende minimums-SOC. En solmulighed kræver to sammenhængende forventede overskudstimer. Manglende prognosedækning frigiver ikke strøm til bilen.
- Husplanen er en heuristisk prognose uden fremtidig Tesla-belastning, ikke en færdig fælles økonomisk optimering. Nettets grænser, Tesla og batteri koordineres endnu ikke automatisk.
- Planen starter ved faktisk SOC, også under reserven. Første times energi reduceres til den resterende del af timen.
- Præcise ladetidsrum frem til afgang; ufuldstændige natpriser vises som foreløbige og markedsføres ikke som billigste plan.
- Husprofilen fratrækker kun valideret Tesla-effekt. Tilsluttede perioder med ukendte, gamle eller fysisk usandsynlige bildata udelades fra læringen.
- Nye målinger gemmer HA-effektsensorens last_updated. Uændrede værdier kan blive markeret som gamle, selv hvis bilen faktisk fortsætter; derfor er dagsenergi et delvist estimat. Ældre historik uden tidsstempel kan ikke valideres bagud.
- Gem Tesla-plan giver lokal bekræftelse, tidspunkt/modtager og fejlstatus. Planfilen erstattes atomisk.
- Seneste ladebesked gemmes og vises i Tesla-kortet. Nye beskeder spejles til HA-notifikationscenteret, og mobilbeskeder linker til add-onens ingress.
- Påmindelser sendes ved hjemkomstvinduet, ikke i det tidlige morgenvindue.
- Analysepakken medtager Tesla-plan, dynamisk reserve, besked og datatidsstempler.
- Automatisk styring er stadig låst fra. Ingen Modbus- eller Tesla-styrekommandoer udføres.

Første sikre trin til en lokal AI-agent på Home Assistant OS.

## Funktioner

- Ingress-panel beskyttet af Home Assistant-login.
- Kontrol af forbindelsen til Home Assistant og Deye/EW11.
- Live læsning af Modbus holding-registre.
- Snapshot og sammenligning af registre.
- Automatisk scanning og korrekt fortolkning af centrale Deye-registre.
- Lokal energiplan med mål-SOC, morgenreserve, opladnings- og eksportgrænse.
- Fast sikkerhedsparameter for husets 3 × 35 A hovedsikringer.
- Shadow Mode logger inverterens tilstand og Copilotens indstillinger.
- Andel Energi FlexEnergi-priser inklusive transport og afgifter samt solprognose fra Open-Meteo.
- Automatisk offentlig spotpris som reserve, hvis Andels prisside er utilgængelig.
- Elpris og vejr hentes uafhængigt, så fejl i den ene tjeneste ikke skjuler den anden.
- Forklarlige forslag med Godkend/Afvis og revisionshistorik.
- Komplet før/efter-snapshot til opdagelse af nyere EV/LoRa-registre.
- Responsivt live-flow med animerede energiprikker for sol, net, batteri og hus.
- Tesla Fleet er kortlagt read-only med kabelstatus, SOC, ladeeffekt, strøm, spænding, ladestatus og 80 %-grænse.
- Tesla Shadow Mode foreslår opladning fra soloverskud eller billige timer og beskytter husets batterireserve om aftenen.
- Tesla Long Range-ladeplanen beregner energibehov og billigste timer frem mod afgang og sender en tilslutningspåmindelse via Home Assistant eller den valgte Companion App.
- Dagens Tesla-opladning beregnes fra den målte ladeeffekt over tid, så en gammel eller akkumuleret Tesla-tæller ikke fejlagtigt vises som dagens kWh.
- Tesla-data medtages i analysepakken; ingen Tesla-kommandoer udføres i denne version.
- Energiflowet viser nu en separat lader mellem inverter og Tesla med grå, rød eller grøn status.
- Tesla-noden viser batteriprocent og beregnet opladet energi for den aktuelle dag.
- Den matte grønne Tesla fra hoveddashboardet vises i bilfeltet, mens kun rammen skifter statusfarve.
- Inverteren er flyttet op, og laderikonet er centreret mellem inverter og bil for bedre læsbarhed.
- Rettet Home Assistant-ingresssti til Tesla-billedet og afgrænset det store flow-SVG, så laderikonet bliver inde i sin firkant.
- Tesla-billedet er indlejret direkte i HTML, så det ikke længere afhænger af filhentning gennem ingress.
- Korrigeret total neteffekt til register 619 og total husbelastning til register 653.
- Netfeltet viser samlet import/eksport med fed skrift samt L1, L2 og L3 nedenunder.
- Live Modbus-data gemmes nu også, hvis elpris- eller vejrtjenesten midlertidigt fejler.
- Dashboardet advarer tydeligt, hvis den seneste måling er mere end to minutter gammel.
- Scannerens interne, eksterne og valgte netregistre 604–625 har fået tydelige navne.
- Udvidet energiflow med PV1/PV2, dagens sol, netimport/-eksport, husforbrug samt batteriets opladning og afladning.
- Time-for-time graf for dagens spotpriser.
- Lokal solprognose for et 10 kWp vestvendt anlæg med justerbar hældning og systemtab.
- Energinet-kaldet bruger nu faste danske datoer og Europe/Copenhagen, så dagens timepriser indlæses stabilt.
- Dagens solprognose kombinerer nu inverterens faktiske produktion med prognosen for de resterende timer.
- Knapper til en privatlivsrenset analysepakke med enten 24 timers eller 7 dages historik.
- Samlet FlexEnergi-købspris med N1's tidsdifferentierede 2026-nettarif samt Energinets tariffer og elafgift. Faste abonnementer er ikke medregnet.

## Sikkerhedsmodel

Denne version er låst til read-only. En godkendelse registreres kun i den lokale historik og
udfører ingen Modbus-skrivning. Deye AI Copilot kan derfor fortsætte som eneste aktive styring.
- Oversigt over Deye-enheder i Home Assistant.
- Read-only: ingen Modbus-skrivning og ingen ændring af Home Assistant-filer.

## Installation

Kopiér hele mappen `deye_ai_bridge` til `/local_apps/deye_ai_bridge` på Home Assistant.
Gå til **Indstillinger > Apps > Installer app**, vælg menuen og **Søg efter opdateringer**.
Installér derefter **Deye AI Bridge**, start den, og aktivér **Vis i sidepanel**.

Angiv inverterens lokale IP-adresse i appens indstillinger før første start. IP-adressen
gemmes kun i Home Assistant og ligger ikke i dette offentlige repository.

Hvis appen ikke vises, kontrollér Supervisor-loggen for fejl i `config.yaml`.

## Vigtigt

EW11-adaptere kan være begrænset til én Modbus-klient. Hvis scanning fejler, mens Home Assistant
poller inverteren, betyder det ikke nødvendigvis, at appen er defekt. En senere version kan koordinere
en kort pause i Home Assistants Modbus-polling efter udtrykkelig godkendelse.

Registerskrivning, eksport til nettet og batterisalg bliver først aktiveret, når registerkortet er
valideret mod den konkrete inverter og firmware. Register 141 er et bitfelt og må ikke behandles som
en simpel 0/1-kontakt.
