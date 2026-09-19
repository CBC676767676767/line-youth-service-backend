import React from "react";
import ReactDOM from "react-dom/client";
import CitizenApp from "./CitizenApp";
import "../styles.css";
import "./citizen.css";
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <CitizenApp />
  </React.StrictMode>,
);
