# Deye AI Bridge 0.9.6

## Målinger og status
Kernemålinger gemmes før langsomme pris-/vejr-/Tesla-opslag og planberegning. Baggrundsopgaver kører særskilt; transportkøen er FIFO. Tidsstempler beskriver starten af målingen, og statuskald viser en frisk enkel måling uden en tung analyseberegning. 120-sekunders sikkerhedsstoppet bevares. Fejlens fulde løsning skal stadig bekræftes på anlægget.

## Opdater uden kode
Åbn **Vedligeholdelse** i dashboardets top. Vælg et forslag eller en manuel JSON-kodepakke, gennemgå ændringen og tryk **Opdater · backup og genstart Bridge**. Dit lokale klik er godkendelsen; ingen kode eller efterfølgende chatgodkendelse kræves. Godkendelsen er bundet til præcis den gennemgåede pakke. Automatikken skal være fra og forbliver fra.

## Reserve når dashboardet fejler
Vedligeholdelsessiden kører i den faste opstartsproces og forbliver tilgængelig, hvis dashboardprocessen ikke starter. Upload en JSON-kodepakke på højst 800 KB manuelt, gennemgå den og opdater. Den samme backup, kontrol og tilbageførsel bruges. En uafklaret installation gentages ikke automatisk; Kontrollér status afklarer resultatet.

En gendannet 120-sekunders fejl kan kvitteres lokalt efter tre nye registerkontroller uden inverterkommandoer. Fremmede værdier eller uafklaret gendannelse blokerer fortsat installation.

## Installation
Denne version kræver én almindelig HA-opdatering med backup, da den ændrer den faste opstartsproces. Kun kendt 0.9.5-kodegrundlag med bekræftet inaktiv/gendannet tilstand migreres automatisk efter tre registerkontroller. Derefter kræver normale Bridge-kodeforslag og manuel upload ikke GitHub. Faste systemafhængigheder/opstartsprocessen kræver stadig særskilte HA-installationspakker ved fremtidige ændringer.

## Validering
139 Python-tests består; én root-rettighedstest kræver et andet testmiljø. Syv simulerede UI-forløb består. Containerbygning og fysisk drift på HA afventer installation og kontrol. Se RELEASE_0.9.6.md for præcis afgrænsning og konsekvenser.
