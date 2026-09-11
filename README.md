# iceberg-selection-committee-vote
Vote polling and counting script for Iceberg Selection Committee 2027

# To set up:
git clone --recurse-submodules https://github.com/sungwy/iceberg-selection-committee-vote
cd iceberg-selection-committee-vote
git submodule add https://github.com/apache/steve
git -C steve checkout 1081d378d612ee2943d90a76adfe7f2c62302c28

# Pre-requisites
1. candidates.tsv: which lists the candidates
2. eligibile.txt: which lists the eligible voters by email address
3. responses.csv: which lists the voting responses (before anonymization)

# Outputs
1. ballots.csv: intermediate output that has anonymized ballots
2. audit.txt: count log and count result

# To anonymize the ballots
python3 stv_committee.py anonymize responses.csv --eligible eligible.txt --out ballots.csv

# To count the ballots
python3 stv_committee.py count ballots.csv candidates.tsv --steve ./steve --seed [seed] --log audit.txt
