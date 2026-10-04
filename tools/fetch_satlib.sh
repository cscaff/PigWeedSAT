#!/bin/sh
# Re-download the SATLIB archives behind benchmarks/satlib/table3 (SAT-Accel paper, Table 3).
set -e
cd "$(dirname "$0")/../benchmarks/satlib"
mkdir -p tgz x
B=https://www.cs.ubc.ca/~hoos/SATLIB/Benchmarks/SAT
for u in RND3SAT/uf100-430.tar.gz RND3SAT/uuf100-430.tar.gz RND3SAT/uf125-538.tar.gz \
         RND3SAT/uuf125-538.tar.gz RND3SAT/uf150-645.tar.gz CBS/CBS_k3_n100_m403_b10.tar.gz \
         DIMACS/AIM/aim.tar.gz DIMACS/PHOLE/pigeon-hole.tar.gz DIMACS/II/inductive-inference.tar.gz; do
  curl -sfL -o "tgz/$(basename $u)" "$B/$u"
  tar xzf "tgz/$(basename $u)" -C x
done
echo "extracted into benchmarks/satlib/x"
