# Deye AI Bridge 0.9.6 – målinger og lokale opdateringer

Forberedt og testet lokalt. Denne version er ikke publiceret eller installeret.

## Den konkrete overgang

Publicér 0.9.6 i det eksisterende GitHub-repository. Brugeren installerer derefter gennem HA's almindelige Opdater-knap med backup og automatikken slået fra. Dette ændrer også den faste opstartsproces og genstarter kun Bridge-appen, ikke HA Core, Pi eller inverteren.

Ved første start accepteres kun det kendte 0.9.5-kodegrundlag med deaktiveret drift eller den kendte 120-sekunders fejl, bekræftet gendannelse og ingen uafklaret kommando eller genaktivering. De oprindelige registerværdier kontrolleres tre gange. Afvigelser blokerer overgangen; der sendes ingen inverterkommando fra migrationen. Backup og fejljournal bevares. Ny kode vælges som en særskilt release; opstartsfejl vælger den tidligere release. Automatikken forbliver fra.

## 1. Målinger skal ikke vente på baggrundsarbejde

- Kernemålinger hentes og gemmes før priser, vejr, Tesla, optional telemetry, planberegning og notifikationer. Disse opgaver er flyttet til en separat baggrundstråd med et enkelt samlende signal, ikke en voksende opgavekø.
- Tidsstemplet sættes før registerbatchen, så en langsom aflæsning ikke kan udgive gamle data som nye. En mislykket kerneaflæsning gemmer ingen falsk frisk måling.
- Måleintervallet regnes fra start til start med højst 60 sekunders interval. Langsom behandling tillægges ikke længere endnu et helt interval.
- Adgang til Modbus er FIFO, så nye styringsaflæsninger ikke løbende overhaler en ventende måleopsamling. Kø- og kommunikationstimeouts bevares.
- SQLite-forbindelser lukkes efter brug; WAL mindsker blokering mellem læsere og skriveren.
- Statuskald giver gemt styringstilstand, måleopsamlingens aktuelle trin og en frisk, enkel måling. Den store analyse hentes fra baggrundscache og mærkes som cache med beregnet alder.
- Gamle Tesla-data bliver ukendte i stedet for stiltiende at blive antaget som nulforbrug. BMS-kontroller, SOC-grænser, registerejerskab, gendannelse og 120-sekunders sikkerhedsstop bevares.

Den tidligere låsrettelse løste ikke hele den observerede målepause. Ovenstående fjerner flere konkrete blokeringer og tilføjer synlig diagnose, men er ikke bevis for stabil fysisk drift. Det skal efterprøves på anlægget under særskilt godkendt, overvåget aktivering.

## 2. Dit lokale klik erstatter godkendelseskoden

- En ny side i den faste opstartsproces viser ventende forslag, beskrivelse, fuld diff, kontrolsum, kodegrundlag og konsekvens.
- Der er intet kodefelt og ingen ekstra godkendelsesrunde i chatten. Knappen **Opdater · backup og genstart Bridge** er den konkrete lokale godkendelse.
- Den lokale side får et kortlivet tilfældigt sidetoken. Gennemgang giver en engangsbillet bundet til den præcise pakkekontrolsum, kodegrundlag og side. Ændret indhold, forkert side, udløbet eller brugt billet afvises.
- Sidetoken og billet udleveres ikke gennem MCP-læse-, forslag- eller preview-værktøjerne. Den tidligere nøgle bliver ikke sendt til browseren eller chatten. Intern godkendelse og installation sker samlet under lås; der efterlades ingen godkendelse, som chatten kan genbruge.
- Denne løsning bygger på tillid til HA-adgangen og den installerede Bridge-kode. Den manuelle nøgle som ekstra adskillelse fra applikationskoden er bevidst erstattet af lokal browsergodkendelse.
- Kontrol af deaktiveret drift, verificeret gendannelse og øvrige installationskrav bevares i den beskyttede kerne. Upload alene ændrer kun venteområdet og genstarter intet.

## 3. Manuel upload og gendannelse

- Samme side har **Manuel opdateringsfil** for en JSON-kodepakke på højst 800 KB. Filen gennemgås i browseren og installeres først ved klik på Opdater. Den samme kontrolliste, pakkebinding, backup og opstartskontrol bruges som ved direkte forslag.
- Den faste opstartsproces ejer port 8099 og vedligeholdelsessiden. Dashboardet kører bag den på loopback-port 8098. Hvis dashboardprocessen stopper eller fejler efter en opdatering, forbliver vedligeholdelse og manuel upload tilgængelig. Der er ingen automatisk genstartsløkke for et nedbrudt dashboard.
- Tilbageførsel til ældre kode bevarer portkompatibilitet gennem en fast start-wrapper.
- En allerede verificeret, gendannet 120-sekunders fejl kan kvitteres med en særskilt tydelig lokal knap. Status skal være uændret, forbindelsen skal matche, og de oprindelige registre skal stemme tre gange. Backup og den tidligere fejl gemmes; ingen inverterkommando sendes, og automatikken startes ikke.
- Hvis selve den faste opstartsproces, operativsystemet eller Python-miljøet er ødelagt, kan dens webside ikke love at reparere det. Det kræver en almindelig HA-installations-/gendannelsesvej.

## 4. Tydelig fremdrift og fejl

Statusfeltet viser modtaget klik, manglende eller blokerende forhold, accepteret installation, opstart, succes eller tilbageførsel. Succes kræver både den godkendte operations pakkekontrolsum, installed-status og den forventede aktive release. Mistet svar betyder statuskontrol, ikke ny installation. En uafklaret installation spærrer for gentagelse; kontrol genoptages efter genindlæsning. Ingen kode eller nøgle gemmes i browseren.

## GitHub fremover

Efter denne overgang kræver almindelige Bridge-kodeopdateringer ikke GitHub: chatten uploader et forslag, eller brugeren vælger en JSON-fil lokalt. Installation kræver stadig dit lokale klik. Den faste opstartsproces, Python-/OS-afhængigheder og HA-appkonfigurationen er ikke en del af den normale kodepakke; eventuelle fremtidige ændringer af dem kræver en særskilt HA-installationspakke. Repository eller GitHub-konto slettes eller lukkes ikke af denne opdatering.

## Kontrol og begrænsninger

139 Python-tests består: 87 eksisterende applikations-/styringstests og 52 tests af vedligeholdelse, lokal godkendelse, migration, låse og måleopsamling. Én yderligere eksisterende test af faktiske root-/procesrettigheder er sprunget over, fordi testmiljøet ikke kører som root.

Syv simulerede browserforløb består: direkte forslag, manuel fil, afvisning, mistet svar, tilbageførsel, forkert aktiv release og aktiv automatik som installationsblokering. En lokal HTTP-test gennemgår og installerer via den uafhængige side uden en dashboardproces. Python- og JavaScript-syntaks samt den ældre ports kompatibilitet er kontrolleret.

Der er ikke Docker i arbejdsmiljøet. Containerbygning, faktisk HA-ingress på mobil og længerevarende fysisk automatisk drift er endnu ikke testet. Efter brugerens HA-opdatering kontrolleres version, gendannelse, målingsalder, måleintervaller, direkte forslag og manuel upload. Automatisk drift startes kun særskilt under opsyn.
