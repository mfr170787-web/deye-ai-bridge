# Deye AI Bridge 0.6.9

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

Version 0.6.1 er fysisk låst til read-only. En godkendelse registreres kun i den lokale historik og
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
