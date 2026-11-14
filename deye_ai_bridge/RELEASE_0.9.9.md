# Home Assistant-app 0.9.9 · opdateringskerne 1.2.0

Vedligeholdelse har nu en **Tilbage til Deye AI Bridge**-knap og et samlet lokalt opdateringsforløb. Manuel JSON-upload bevares.

Dit lokale klik godkender den præcise gennemgåede pakke og:

1. Pause af tidligere aktiv drift og bekræftet gendannelse.
2. Backup, installation og genstart af kun Bridge-processen.
3. Kontrol af den aktive pakke og en ny, aktuel måling.
4. Genoptagelse af tidligere aktiv drift, når cloudstyring er bekræftet frakoblet.
5. Mindst 125 sekunders efterkontrol med nye målinger.

Drift, der var deaktiveret, forbliver deaktiveret. Manuelt stop annullerer genoptagelse. Andre ændringer blokeres under forløbet. Fejl eller afbrydelse giver ingen yderligere genoptagelse. Hvis aktivering er forsøgt, anmodes der om stop og kontrolleres for bekræftet gendannelse. Hvis det ikke kan bekræftes, fremgår det udtrykkeligt. Mistede styresvar gensendes ikke automatisk. Et afbrudt forløb fortsættes ikke automatisk efter kernens genstart.

## Denne engangsopdatering

**0.9.9 installeres via Home Assistants normale appopdatering. Før den installeres, skal automatisk drift deaktiveres, og gendannelsen skal være bekræftet i den eksisterende version.** Den gamle kerne kan ikke anvende det nye forløb, før den nye kerne er installeret. HA-opdateringen genstarter appens container.

Denne udgivelse ændrer kun opdateringskernen og dens side. Den bevarer den aktive Bridge-kode, konfiguration og data. Visningen kan derfor stadig vise aktiv appkode 0.9.6, samtidig med at Home Assistant viser appversion 0.9.9 og vedligeholdelsessiden viser opstartsproces 1.2.0. Forslag 0.9.8 med ladeplanen installeres separat fra vedligeholdelsessiden. Automatik genaktiveres ikke af selve denne engangsopdatering.

Ingen ændring af inverterens strømgrænser, SOC-grænser, 120-sekunders sikkerhedsstop eller automatisk EV-start/stop. Almindelige appforslag kan efterfølgende installeres gennem det nye lokale forløb; fremtidige ændringer i selve den beskyttede kerne kræver fortsat en containeropdatering.

## Validering

44 lokale kernetests kørt, heraf én eksisterende miljøafhængig test sprunget over. 96 app-/plantests består. UI-test dækker begge vedligeholdelsesruter, tilbageknap gennem Home Assistant-ingress, lokal afkrydsning, fremdrift, fejl og afslutning. Python- og JavaScript-syntakskontrol består. Ingen fysisk invertertest eller containerbygning er udført i dette miljø.
