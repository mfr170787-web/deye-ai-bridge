# Deye AI Bridge 0.9.5

Lokal Deye-styring og opdateringer med tydelig status.

## Opdatering fra den fastlåste 120-sekunders fejl

Opdatér den eksisterende app gennem Home Assistant med backup, mens automatikken er slået fra. Installationspakken kontrollerer den kendte gamle kode og fejlstatus og læser de oprindelige inverterregistre tre gange. Kun ved bekræftet gendannelse kvitteres fejlen, med bevaret fejljournal og backup. Automatikken forbliver deaktiveret. Der sendes ingen inverterkommandoer fra gendannelsesmigrationen.

Ved afvigelser, ukendt kodegrundlag eller forbindelsesfejl vælges den tidligere kode; se appens log og vedligeholdelsesstatus. En blokeret migration gentages ikke automatisk. Efter opdateringen skal aktiv kodeversion 0.9.5, gendannelsesresultat og friske målinger kontrolleres før særskilt genaktivering.

## Rettelser

- Statusaflæsning blokerer ikke måleopsamlingen på styringens lås.
- Deaktivér og gendan kan kvittere en allerede verificeret fejl efter tre nye registerkontroller; fejlen bevares i journalen.
- Vedligeholdelse viser modtaget klik, manglende oplysninger, installationsblokering, opstart, bekræftet succes og fejl. Mistet svar giver statuskontrol uden ny installation.
- Aktuel analyse, ventende kodeforslag og den korrigerede reserveberegning indgår i installationspakken.

120-sekunders sikkerhedsgrænse, lokal godkendelsesnøgle og kravet om særskilt aktivering af automatisk drift bevares. Opdateringen stopper og starter kun denne Bridge-app.
