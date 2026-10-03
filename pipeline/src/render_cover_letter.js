/**
 * Renders a cover letter JSON into a formatted .docx.
 *
 * Handles two shapes:
 *   grouped (current) - greeting, opening, positioning, lead_in,
 *                       groups[{header, bullets[]}], closing_para, sign_off
 *   prose   (legacy)  - greeting, paragraphs[], closing
 *
 * Usage: node render_cover_letter.js <cover.json> <output.docx>
 */
const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
} = require("docx");

const [, , inputPath, outputPath] = process.argv;
if (!inputPath || !outputPath) {
  console.error("Usage: node render_cover_letter.js <cover.json> <output.docx>");
  process.exit(1);
}

const letter = JSON.parse(fs.readFileSync(inputPath, "utf8"));
const children = [];
const para = (text, opts = {}) => new Paragraph({ text, spacing: { after: 200 }, ...opts });

// header
children.push(new Paragraph({ text: letter.name || "", heading: HeadingLevel.TITLE }));
if (letter.contact) {
  for (const line of letter.contact.split("\n").filter(Boolean)) {
    children.push(new Paragraph({
      children: [new TextRun({ text: line, size: 20, color: "555555" })],
    }));
  }
}
children.push(new Paragraph({ text: "", spacing: { after: 200 } }));

if (letter.date) children.push(para(letter.date));
for (const line of (letter.recipient || "").split("\n").filter(Boolean)) {
  children.push(new Paragraph({ text: line }));
}
if (letter.greeting) {
  children.push(para(letter.greeting, { spacing: { before: 300, after: 200 } }));
}

if (letter.groups && letter.groups.length) {
  // grouped format
  if (letter.opening) children.push(para(letter.opening));
  if (letter.positioning) children.push(para(letter.positioning));
  if (letter.lead_in) children.push(para(letter.lead_in, { spacing: { after: 150 } }));

  for (const g of letter.groups) {
    if (g.header) {
      children.push(new Paragraph({
        children: [new TextRun({ text: g.header, bold: true })],
        spacing: { before: 200, after: 80 },
      }));
    }
    for (const b of g.bullets || []) {
      children.push(new Paragraph({ text: b, bullet: { level: 0 }, spacing: { after: 60 } }));
    }
  }

  if (letter.closing_para) {
    children.push(para(letter.closing_para, { spacing: { before: 300, after: 200 } }));
  }
} else {
  // legacy prose format
  for (const p of letter.paragraphs || []) {
    children.push(para(p, { alignment: AlignmentType.LEFT }));
  }
}

children.push(new Paragraph({
  text: letter.sign_off || letter.closing || "Sincerely,",
  spacing: { before: 200 },
}));
children.push(new Paragraph({ text: letter.name || "", spacing: { before: 300 } }));

const doc = new Document({
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 }, // US Letter
        margin: { top: 1080, bottom: 1080, left: 1080, right: 1080 },
      },
    },
    children,
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(outputPath, buf);
  console.log(`Wrote ${outputPath}`);
});
