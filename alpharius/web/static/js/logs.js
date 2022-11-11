const elem = document.getElementById("datepicker");
const tz_offset = new Date().getTimezoneOffset() * 60000;
const min_date = Date.parse(DATES[0]) + tz_offset;
const max_date = Date.parse(DATES[DATES.length - 1]) + tz_offset;
const date_set = new Set();
for (d of DATES) {
    date_set.add(Date.parse(d) + tz_offset);
}
var dates_disabled = [];
for (var d = min_date; d <= max_date; d += 86400000) {
    if (!date_set.has(d)) {
        dates_disabled.push(d);
    }
}
const datepicker = new Datepicker(elem, {
    autohide: true,
    format: "M dd yyyy",
    minDate: min_date,
    maxDate: max_date,
    datesDisabled: dates_disabled,
});
datepicker.setDate(Date.parse(CURRENT_DATE) + tz_offset);
elem.addEventListener("changeDate", function(event){
    location.href = "logs?date=" + datepicker.getDate("yyyy-mm-dd");
});

const logger_select = document.getElementById("logger-select");
logger_select.addEventListener("change", function(event){
    var logger = event.target.value;
    document.getElementById("log-" + current_logger).style.display = "none";
    document.getElementById("log-" + logger).style.removeProperty("display");
    current_logger = logger;
});

// Back-to-top button
let btt_button = document.getElementById("btn-back-to-top");
let gtb_button = document.getElementById("btn-go-to-bottom");
window.onscroll = function () {
    scrollFunction();
};
function scrollFunction() {
    if (document.documentElement.scrollTop > 20) {
        btt_button.style.display = "block";
    } else {
        btt_button.style.display = "none";
    }
    if (document.documentElement.scrollTop < document.documentElement.scrollHeight - window.innerHeight - 20) {
        gtb_button.style.display = "block";
    } else {
        gtb_button.style.display = "none";
    }
}
btt_button.addEventListener("click", backToTop);
gtb_button.addEventListener("click", goToBottom);
function backToTop() {
    document.documentElement.scrollTop = 0;
}
function goToBottom() {
    document.documentElement.scrollTop = document.documentElement.scrollHeight - window.innerHeight;
}