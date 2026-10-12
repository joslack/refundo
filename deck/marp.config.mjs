// Marp reads this file when it is run from deck/.
//
// When the deck is presented from the browser, each row of a table appears on its own key press, as the items of
// a list written with "*" do. A PDF or an image of a slide shows every row. A slide with the class "whole"
// (<!-- _class: whole -->) shows its table all at once: use it for a table that is read by comparing its rows.
const rowsAppearOneByOne = (md) => {
  md.core.ruler.push('table_rows_as_fragments', (state) => {
    let row = 0
    let inBody = false
    let whole = false
    for (const token of state.tokens) {
      if (token.type === 'marpit_slide_open') {
        row = 0
        whole = /\bwhole\b/.test(token.attrGet('class') || '')
      }
      else if (token.type === 'tbody_open') inBody = true
      else if (token.type === 'tbody_close') inBody = false
      else if (token.type === 'tr_open' && inBody && !whole) token.attrSet('data-marpit-fragment', String(++row))
    }
  })
}

// html: a stage of a rail can be given data-marpit-fragment="n" in slides.md to appear on the nth key press. Marp
// drops that attribute from the page unless HTML is allowed in full.
export default { html: true, engine: ({ marp }) => marp.use(rowsAppearOneByOne) }
