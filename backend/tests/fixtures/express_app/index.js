const express = require("express");

const app = express();

app.get("/", (req, res) => {
  res.json({ message: "Hello from DockerForge fixture!" });
});

app.get("/health", (req, res) => {
  res.json({ status: "ok" });
});

const port = process.env.PORT || 3000;
app.listen(port, () => {
  console.log(`Server listening on port ${port}`);
});
