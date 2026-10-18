# TouhouCCB Rulebook -- LaTeX build
#
# Targets:
#   make        : build rulebook.pdf
#   make fx     : build fx-rulebook.pdf
#   make trading-guide : build trading-system-guide.pdf
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

fx: fx-rulebook.pdf

fx-rulebook.pdf: fx-rulebook.tex fxrulebook.sty touhouccb.sty
	$(LATEX) $(LATEXOPTS) fx-rulebook.tex
	$(LATEX) $(LATEXOPTS) fx-rulebook.tex

trading-guide: trading-system-guide.pdf

trading-system-guide.pdf: trading-system-guide.tex touhouccb.sty
	$(LATEX) $(LATEXOPTS) trading-system-guide.tex
	$(LATEX) $(LATEXOPTS) trading-system-guide.tex

clean:
	@for ext in $(AUX_EXTS); do rm -f *.$$ext; done

distclean: clean
	rm -f $(PDF)

.PHONY: all fx trading-guide clean distclean
