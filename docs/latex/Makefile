# TouhouCCB Rulebook -- LaTeX build
#
# Targets:
#   make        : build rulebook.pdf
#   make clean  : remove intermediate files
#   make distclean : remove everything including the PDF

LATEX     = xelatex
LATEXOPTS = -interaction=nonstopmode -halt-on-error -file-line-error
SRC       = rulebook.tex
PDF       = rulebook.pdf
AUX_EXTS  = aux log toc out fls fdb_latexmk synctex.gz nav snm vrb

all: $(PDF)

# Run xelatex twice for stable ToC / cross-refs.
$(PDF): $(SRC)
	$(LATEX) $(LATEXOPTS) $(SRC)
	$(LATEX) $(LATEXOPTS) $(SRC)

clean:
	@for ext in $(AUX_EXTS); do rm -f *.$$ext; done

distclean: clean
	rm -f $(PDF)

.PHONY: all clean distclean
