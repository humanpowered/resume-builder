/**
 * Renders a tailored resume JSON (produced by score_and_tailor.py) into a
 * clean, formatted .docx.
 *
 * Usage: node render_resume.js <resume.json> <output.docx>
 */
const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel,
  AlignmentType, BorderStyle,
} = require("docx");

const [, , inputPath, outputPath] = process.argv;
if (!inputPath || !outputPath) {
  console.error("Usage: node render_resume.js <resume.json> <output.docx>");
  process.exit(1);
}

const resume = JSON.parse(fs.readFileSync(inputPath, "utf8"));

const HAIRLINE = { style: BorderStyle.SINGLE, size: 6, color: "999999" };

const children = [];

children.push(new Paragraph({
  text: resume.name || "",
  heading: HeadingLevel.TITLE,
  alignment: AlignmentType.CENTER,
}));

children.push(new Paragraph({
  children: [new TextRun({ text: resume.contact || "", size: 20, color: "555555" })],
  alignment: AlignmentType.CENTER,
  spacing: { after: 300 },
}));

if (resume.summary) {
  children.push(new Paragraph({
    children: [new TextRun({ text: resume.summary, italics: true })],
    spacing: { after: 300 },
  }));
}

// keepNext binds a paragraph to the one after it, so Word will push both to
// the next page rather than stranding a heading alone at the bottom.
function sectionHeading(text) {
  return new Paragraph({
    children: [new TextRun({ text, bold: true, size: 24 })],
    border: { bottom: HAIRLINE },
    spacing: { before: 200, after: 150 },
    keepNext: true,
  });
}

if (resume.experience && resume.experience.length) {
  children.push(sectionHeading("EXPERIENCE"));
  for (const job of resume.experience) {
    children.push(new Paragraph({
      children: [
        new TextRun({ text: job.title || "", bold: true }),
        new TextRun({ text: job.company ? `  |  ${job.company}` : "", bold: true }),
      ],
      spacing: { before: 150 },
      keepNext: true,        // title stays with its dates
      keepLines: true,
    }));
    if (job.dates) {
      children.push(new Paragraph({
        children: [new TextRun({ text: job.dates, italics: true, size: 20, color: "555555" })],
        keepNext: true,      // dates stay with the first bullet
      }));
    }
    const bullets = job.bullets || [];
    bullets.forEach((bullet, i) => {
      children.push(new Paragraph({
        text: bullet,
        bullet: { level: 0 },
        keepLines: true,     // a bullet never splits across pages
        // hold the first two bullets with the header block, so a role never
        // starts with just its title showing at the foot of a page
        keepNext: i === 0 && bullets.length > 1,
      }));
    });
  }
}

if (resume.skills && resume.skills.length) {
  children.push(sectionHeading("SKILLS"));
  // Two shapes are supported. A flat list of short skills becomes one
  // comma-separated line (commas parse more reliably than bullet glyphs).
  // Entries shaped "Category: a, b, c" each get their own paragraph, or
  // they run together into an unreadable blob.
  // Grouped only when EVERY entry is a "Category: items" line, as the
  // general resume builds them. Using .some() here meant a flat tailored
  // list bolded the few entries that happened to contain a colon
  // ("Unit economics: CAC, ...") and left the rest plain.
  const catLine = /^[^:]{3,40}:\s/;
  const grouped = resume.skills.length >= 3 && resume.skills.every((s) => catLine.test(s));
  if (grouped) {
    for (const line of resume.skills) {
      const i = line.indexOf(":");
      children.push(new Paragraph({
        children: [
          new TextRun({ text: line.slice(0, i + 1) + " ", bold: true }),
          new TextRun({ text: line.slice(i + 1).trim() }),
        ],
        spacing: { after: 60 },
      }));
    }
  } else {
    children.push(new Paragraph({ text: resume.skills.join(", ") }));
  }
}

if (resume.education && resume.education.length) {
  children.push(sectionHeading("EDUCATION"));
  for (const edu of resume.education) {
    children.push(new Paragraph({ text: edu }));
  }
}

const doc = new Document({
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 }, // US Letter
        margin: { top: 720, bottom: 720, left: 900, right: 900 },
      },
    },
    children,
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(outputPath, buf);
  console.log(`Wrote ${outputPath}`);
});
