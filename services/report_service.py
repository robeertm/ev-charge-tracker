"""PDF Report Generator for EV Charge Tracker."""
import io
import os
import tempfile
from datetime import date

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from fpdf import FPDF

from models.database import db, Charge, AppConfig, ThgQuota
from services.stats_service import (
    get_summary_stats, get_monthly_stats, get_yearly_stats,
    get_ac_dc_stats, get_chart_data,
)

# Colors
C_PRIMARY = '#2196F3'
C_SUCCESS = '#4CAF50'
C_WARNING = '#FF9800'
C_DANGER = '#F44336'
C_INFO = '#00BCD4'
C_DARK = '#333333'
C_LIGHT = '#F5F5F5'
C_PV = '#03A9F4'

plt.rcParams.update({
    'figure.facecolor': 'white',
    'axes.facecolor': 'white',
    'axes.grid': True,
    'grid.alpha': 0.3,
    'font.size': 9,
})


def _save_chart(fig, tmp_dir, name):
    path = os.path.join(tmp_dir, f'{name}.png')
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return path


def _generate_charts(stats, monthly, chart_data, acdc, tmp_dir):
    """Generate all chart images and return dict of paths."""
    charts = {}
    labels = chart_data['monthly_labels']

    # 1. Monthly costs with average
    fig, ax = plt.subplots(figsize=(8, 3))
    costs = chart_data['monthly_cost']
    ax.bar(labels, costs, color=C_PRIMARY, alpha=0.8)
    if costs:
        avg = sum(costs) / len(costs)
        ax.axhline(avg, color=C_DANGER, linestyle='--', linewidth=1.5, label=f'Ø {avg:.2f} €')
        ax.legend()
    ax.set_ylabel('€')
    ax.set_title('Monatliche Ladekosten', fontweight='bold')
    plt.xticks(rotation=45, ha='right')
    charts['monthly_cost'] = _save_chart(fig, tmp_dir, 'monthly_cost')

    # 2. Monthly kWh with average
    fig, ax = plt.subplots(figsize=(8, 3))
    kwh = chart_data['monthly_kwh']
    ax.bar(labels, kwh, color=C_SUCCESS, alpha=0.8)
    if kwh:
        avg = sum(kwh) / len(kwh)
        ax.axhline(avg, color=C_DANGER, linestyle='--', linewidth=1.5, label=f'Ø {avg:.1f} kWh')
        ax.legend()
    ax.set_ylabel('kWh')
    ax.set_title('Monatlich geladene kWh', fontweight='bold')
    plt.xticks(rotation=45, ha='right')
    charts['monthly_kwh'] = _save_chart(fig, tmp_dir, 'monthly_kwh')

    # 3. Monthly CO2 with average
    fig, ax = plt.subplots(figsize=(8, 3))
    co2 = chart_data['monthly_co2']
    ax.bar(labels, co2, color=C_WARNING, alpha=0.8)
    if co2:
        avg = sum(co2) / len(co2)
        ax.axhline(avg, color=C_DANGER, linestyle='--', linewidth=1.5, label=f'Ø {avg:.2f} kg')
        ax.legend()
    ax.set_ylabel('kg CO₂')
    ax.set_title('Monatliche CO₂-Emissionen', fontweight='bold')
    plt.xticks(rotation=45, ha='right')
    charts['monthly_co2'] = _save_chart(fig, tmp_dir, 'monthly_co2')

    # 4. Cumulative cost
    fig, ax = plt.subplots(figsize=(8, 3))
    cum_labels = chart_data['cumulative_labels']
    ax.fill_between(range(len(cum_labels)), chart_data['cumulative_cost'], alpha=0.3, color=C_DANGER)
    ax.plot(range(len(cum_labels)), chart_data['cumulative_cost'], color=C_DANGER, linewidth=2)
    ax.set_xticks(range(0, len(cum_labels), max(1, len(cum_labels)//12)))
    ax.set_xticklabels([cum_labels[i] for i in range(0, len(cum_labels), max(1, len(cum_labels)//12))], rotation=45, ha='right')
    ax.set_ylabel('€')
    ax.set_title('Kumulierte Ladekosten', fontweight='bold')
    charts['cumulative_cost'] = _save_chart(fig, tmp_dir, 'cumulative_cost')

    # 5. Cumulative kWh
    fig, ax = plt.subplots(figsize=(8, 3))
    ax.fill_between(range(len(cum_labels)), chart_data['cumulative_kwh'], alpha=0.3, color=C_SUCCESS)
    ax.plot(range(len(cum_labels)), chart_data['cumulative_kwh'], color=C_SUCCESS, linewidth=2)
    ax.set_xticks(range(0, len(cum_labels), max(1, len(cum_labels)//12)))
    ax.set_xticklabels([cum_labels[i] for i in range(0, len(cum_labels), max(1, len(cum_labels)//12))], rotation=45, ha='right')
    ax.set_ylabel('kWh')
    ax.set_title('Kumuliert geladene kWh', fontweight='bold')
    charts['cumulative_kwh'] = _save_chart(fig, tmp_dir, 'cumulative_kwh')

    # 6. CO2 Savings vs Verbrenner (Break-Even)
    fig, ax = plt.subplots(figsize=(8, 3.5))
    savings = chart_data['cumulative_co2_savings']
    battery_co2 = chart_data['battery_production_co2']
    ax.plot(range(len(cum_labels)), savings, color=C_SUCCESS, linewidth=2, label='CO₂-Ersparnis')
    ax.axhline(battery_co2, color=C_DANGER, linestyle='--', linewidth=1.5, label=f'Akkuproduktion ({battery_co2:.0f} kg)')
    ax.fill_between(range(len(cum_labels)), savings, alpha=0.2, color=C_SUCCESS)
    ax.set_xticks(range(0, len(cum_labels), max(1, len(cum_labels)//12)))
    ax.set_xticklabels([cum_labels[i] for i in range(0, len(cum_labels), max(1, len(cum_labels)//12))], rotation=45, ha='right')
    ax.set_ylabel('kg CO₂')
    ax.set_title('CO₂-Ersparnis vs. Verbrenner (Break-Even)', fontweight='bold')
    ax.legend()
    charts['co2_breakeven'] = _save_chart(fig, tmp_dir, 'co2_breakeven')

    # 7. AC/DC/PV Pie chart
    if acdc:
        fig, axes = plt.subplots(1, 3, figsize=(8, 3))
        colors_map = {'AC': C_SUCCESS, 'DC': C_WARNING, 'PV': C_PV}
        for i, (metric, title, unit) in enumerate([
            ('total_kwh', 'kWh', 'kWh'), ('total_cost', 'Kosten', '€'), ('count', 'Ladevorgänge', '')
        ]):
            vals = []
            lbls = []
            cols = []
            for ct in ['AC', 'DC', 'PV']:
                if ct in acdc:
                    vals.append(acdc[ct][metric])
                    lbls.append(f'{ct}: {acdc[ct][metric]}{" " + unit if unit else ""}')
                    cols.append(colors_map[ct])
            if vals:
                axes[i].pie(vals, labels=lbls, colors=cols, autopct='%1.0f%%', textprops={'fontsize': 7})
                axes[i].set_title(title, fontweight='bold', fontsize=9)
        plt.tight_layout()
        charts['acdc_pie'] = _save_chart(fig, tmp_dir, 'acdc_pie')

    # 8. Cost per kWh trend
    fig, ax = plt.subplots(figsize=(8, 2.5))
    cpk = chart_data['monthly_cost_per_kwh']
    ax.plot(labels, cpk, color=C_INFO, linewidth=2, marker='o', markersize=3)
    if cpk:
        avg = sum(cpk) / len(cpk)
        ax.axhline(avg, color=C_DANGER, linestyle='--', linewidth=1, label=f'Ø {avg:.2f} €/kWh')
        ax.legend()
    ax.set_ylabel('€/kWh')
    ax.set_title('Preisentwicklung pro kWh', fontweight='bold')
    plt.xticks(rotation=45, ha='right')
    charts['price_trend'] = _save_chart(fig, tmp_dir, 'price_trend')

    # 9. Monthly charge count
    fig, ax = plt.subplots(figsize=(8, 2.5))
    counts = chart_data['monthly_count']
    ax.bar(labels, counts, color=C_INFO, alpha=0.8)
    ax.set_ylabel('Anzahl')
    ax.set_title('Ladevorgänge pro Monat', fontweight='bold')
    plt.xticks(rotation=45, ha='right')
    charts['monthly_count'] = _save_chart(fig, tmp_dir, 'monthly_count')

    # 10. Yearly comparison bar chart
    yearly = get_yearly_stats()
    if len(yearly) > 1:
        fig, ax = plt.subplots(figsize=(8, 3))
        years = [str(y['year']) for y in yearly]
        x = range(len(years))
        w = 0.25
        ax.bar([i - w for i in x], [y['kwh'] for y in yearly], w, label='kWh', color=C_SUCCESS)
        ax.bar(x, [y['cost'] for y in yearly], w, label='Kosten €', color=C_PRIMARY)
        ax.bar([i + w for i in x], [y['co2'] for y in yearly], w, label='CO₂ kg', color=C_WARNING)
        ax.set_xticks(x)
        ax.set_xticklabels(years)
        ax.set_title('Jahresvergleich', fontweight='bold')
        ax.legend()
        charts['yearly_comparison'] = _save_chart(fig, tmp_dir, 'yearly_comparison')

    return charts


class EVReport(FPDF):
    @staticmethod
    def _clean(text):
        """Replace Unicode chars that latin-1 can't handle."""
        return str(text).replace('—', '-').replace('–', '-').replace('·', '-').replace('²', '2').replace('₂', '2').replace('Ø', 'O').replace('€', 'EUR ')

    def header(self):
        self.set_font('Helvetica', 'B', 14)
        self.set_text_color(33, 33, 33)
        self.cell(0, 8, 'EV Charge Tracker - Report', align='C', new_x='LMARGIN', new_y='NEXT')
        self.set_font('Helvetica', '', 8)
        self.set_text_color(120, 120, 120)
        car = AppConfig.get('car_model', 'EV')
        self.cell(0, 5, self._clean(f'{car} - Erstellt am {date.today().strftime("%d.%m.%Y")}'), align='C', new_x='LMARGIN', new_y='NEXT')
        self.ln(3)

    def footer(self):
        self.set_y(-15)
        self.set_font('Helvetica', 'I', 7)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f'Seite {self.page_no()}/{{nb}} - EV Charge Tracker', align='C')

    def section_title(self, title):
        self.set_font('Helvetica', 'B', 11)
        self.set_text_color(33, 150, 243)
        self.cell(0, 8, self._clean(title), new_x='LMARGIN', new_y='NEXT')
        self.set_draw_color(33, 150, 243)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(3)

    def kpi_table(self, rows_data):
        """Render KPI overview as a clean table.
        rows_data = list of lists of (label, value, unit) tuples.
        Each inner list becomes one row pair (header + value)."""
        total_w = self.w - self.l_margin - self.r_margin
        for items in rows_data:
            n = len(items)
            col_w = total_w / n
            # Label row
            self.set_font('Helvetica', '', 7)
            self.set_text_color(120, 120, 120)
            self.set_fill_color(245, 245, 245)
            for label, _, _ in items:
                self.cell(col_w, 5, self._clean(label), border='TLR', align='C', fill=True)
            self.ln()
            # Value row
            self.set_font('Helvetica', 'B', 9)
            self.set_text_color(33, 33, 33)
            for _, value, unit in items:
                txt = f'{value} {unit}'.strip()
                self.cell(col_w, 6, self._clean(txt), border='BLR', align='C')
            self.ln(8)

    def add_table(self, headers, rows, col_widths=None):
        if not col_widths:
            col_widths = [(self.w - self.l_margin - self.r_margin) / len(headers)] * len(headers)
        # Header
        self.set_font('Helvetica', 'B', 7)
        self.set_fill_color(33, 150, 243)
        self.set_text_color(255, 255, 255)
        for i, h in enumerate(headers):
            self.cell(col_widths[i], 6, self._clean(h), border=1, fill=True, align='C')
        self.ln()
        # Rows
        self.set_font('Helvetica', '', 7)
        self.set_text_color(33, 33, 33)
        for j, row in enumerate(rows):
            if j % 2 == 0:
                self.set_fill_color(245, 245, 245)
            else:
                self.set_fill_color(255, 255, 255)
            for i, cell in enumerate(row):
                self.cell(col_widths[i], 5, self._clean(str(cell)), border=1, fill=True, align='C')
            self.ln()

    def add_chart(self, path, w=180):
        if os.path.exists(path):
            if self.get_y() + 60 > self.h - 20:
                self.add_page()
            self.image(path, x=(self.w - w) / 2, w=w)
            self.ln(3)


def generate_report():
    """Generate a full PDF report. Returns bytes."""
    stats = get_summary_stats()
    if not stats:
        return None

    monthly = get_monthly_stats()
    yearly = get_yearly_stats()
    acdc = get_ac_dc_stats()
    chart_data = get_chart_data()

    tmp_dir = tempfile.mkdtemp()
    charts = _generate_charts(stats, monthly, chart_data, acdc, tmp_dir)

    pdf = EVReport()
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # === Page 1: Overview KPIs ===
    pdf.section_title('Gesamtübersicht')

    kpi_rows = [
        [
            ('Ladekosten', f"EUR {stats['total_cost']:,.0f}", ''),
            ('Geladen', f"{stats['total_kwh']:,.0f}", 'kWh'),
            ('Ladevorgaenge', str(stats['total_charges']), ''),
            ('CO2', f"{stats['total_co2_kg']:,.1f}", 'kg'),
        ],
        [
            ('O Preis', f"EUR {stats['avg_eur_per_kwh']:.2f}", '/kWh'),
            ('CO2-Ersparnis', f"{stats['co2_savings_pct']:.0f}", '%'),
            ('THG-Quote', f"EUR {stats['total_thg_eur']:,.0f}", ''),
            ('Netto-Kosten', f"EUR {stats['net_cost']:,.0f}", ''),
        ],
    ]
    if stats.get('total_km', 0) > 0:
        kpi_rows.append([
            ('Tachostand', f"{stats['total_km']:,.0f}", 'km'),
            ('Verbrauch', f"{stats['consumption_with_recup']:.1f}", 'kWh/100km'),
            ('Kosten/100km', f"EUR {stats['cost_per_100km']:.2f}", ''),
            ('Rekuperation', f"{stats['total_recuperation']:,.0f}", 'kWh'),
        ])
        kpi_rows.append([
            ('Ladezyklen', str(stats['charge_cycles']), ''),
            ('Rekup-Zyklen', str(stats['recup_cycles']), ''),
            ('km durch Rekup.', f"{stats['recup_extra_km']:,}", 'km'),
            ('Netto/100km', f"EUR {stats['net_cost_per_100km']:.2f}", ''),
        ])
    pdf.kpi_table(kpi_rows)

    first = stats.get('first_charge')
    last = stats.get('last_charge')
    if first and last:
        pdf.set_font('Helvetica', 'I', 8)
        pdf.set_text_color(120, 120, 120)
        pdf.cell(0, 5, pdf._clean(f'Zeitraum: {first.strftime("%d.%m.%Y")} bis {last.strftime("%d.%m.%Y")}'), align='C', new_x='LMARGIN', new_y='NEXT')
        pdf.ln(3)

    # === Charts ===
    pdf.section_title('Monatliche Ladekosten')
    pdf.add_chart(charts.get('monthly_cost', ''))

    pdf.section_title('Monatlich geladene kWh')
    pdf.add_chart(charts.get('monthly_kwh', ''))

    pdf.section_title('Monatliche CO₂-Emissionen')
    pdf.add_chart(charts.get('monthly_co2', ''))

    pdf.add_page()
    pdf.section_title('Kumulierte Ladekosten')
    pdf.add_chart(charts.get('cumulative_cost', ''))

    pdf.section_title('Kumuliert geladene kWh')
    pdf.add_chart(charts.get('cumulative_kwh', ''))

    pdf.section_title('CO₂-Ersparnis vs. Verbrenner')
    pdf.add_chart(charts.get('co2_breakeven', ''))

    pdf.add_page()
    pdf.section_title('Preisentwicklung')
    pdf.add_chart(charts.get('price_trend', ''))

    pdf.section_title('Ladevorgänge pro Monat')
    pdf.add_chart(charts.get('monthly_count', ''))

    if 'acdc_pie' in charts:
        pdf.section_title('AC / DC / PV Vergleich')
        pdf.add_chart(charts.get('acdc_pie', ''))

    if 'yearly_comparison' in charts:
        pdf.section_title('Jahresvergleich')
        pdf.add_chart(charts.get('yearly_comparison', ''))

    # === Tables ===
    pdf.add_page()
    pdf.section_title('AC / DC / PV Statistik')
    if acdc:
        headers = ['Typ', 'Anzahl', 'kWh', 'Kosten €', 'Ø €/kWh', 'Ø kWh/Ladung', 'Ø Verlust %']
        rows = []
        for ct in ['AC', 'DC', 'PV']:
            if ct in acdc:
                a = acdc[ct]
                rows.append([ct, str(a['count']), f"{a['total_kwh']:.1f}", f"{a['total_cost']:.2f}",
                             f"{a['avg_eur_per_kwh']:.2f}", f"{a['avg_kwh_per_charge']:.1f}", f"{a['avg_loss_pct']:.1f}"])
        pdf.add_table(headers, rows)
    pdf.ln(5)

    pdf.section_title('Jahresübersicht')
    if yearly:
        headers = ['Jahr', 'Anzahl', 'kWh', 'Kosten €', 'CO₂ kg', 'THG €', 'Netto €']
        rows = [[str(y['year']), str(y['count']), f"{y['kwh']:.0f}", f"{y['cost']:.0f}",
                 f"{y['co2']:.0f}", f"{y['thg']:.0f}", f"{y['net_cost']:.0f}"] for y in yearly]
        pdf.add_table(headers, rows)
    pdf.ln(5)

    pdf.section_title('Monatsübersicht')
    if monthly:
        headers = ['Monat', 'Anzahl', 'kWh', 'Kosten €', 'CO₂ kg', 'Ø €/kWh', 'km', 'Verlust %']
        rows = [[m['label'], str(m['count']), f"{m['kwh']:.0f}", f"{m['cost']:.0f}",
                 f"{m['co2']:.0f}", f"{m['cost_per_kwh']:.2f}", str(m['km']), f"{m['avg_loss_pct']:.1f}"]
                for m in monthly]
        cw = [22, 16, 20, 22, 20, 22, 22, 22]
        pdf.add_table(headers, rows, cw)

    # Cleanup temp files
    for f in os.listdir(tmp_dir):
        try:
            os.remove(os.path.join(tmp_dir, f))
        except Exception:
            pass
    try:
        os.rmdir(tmp_dir)
    except Exception:
        pass

    return pdf.output()
