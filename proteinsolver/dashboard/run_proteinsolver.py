import logging
import queue
import threading
from enum import Enum
from functools import lru_cache, partial

import ipywidgets as widgets
import torch
from IPython.display import HTML, display

from proteinsolver.dashboard import global_state
from proteinsolver.dashboard.download_button import create_download_button
from proteinsolver.dashboard.gpu_status import create_gpu_status_widget
from proteinsolver.dashboard.msa_view import MSASeq
from proteinsolver.dashboard.ps_process import ProteinSolverProcess
from proteinsolver.utils import AMINO_ACID_TO_IDX

logger = logging.getLogger(__name__)


class State(Enum):
    ENABLED = 0
    DISABLED = 1


button_states = {
    State.ENABLED: {
        "description": "Run ProteinSolver!",
        "icon": "check",
        "button_style": "",
        "tooltip": "Generate new sequences!",
    },
    State.DISABLED: {
        "description": "Cancel",
        "icon": "ban",
        "button_style": "danger",
        "tooltip": "Cancel!",
    },
}


class ProteinSolverThread(threading.Thread):
    def __init__(self, progress_bar, run_ps_status_out, msa_view, run_proteinsolver_button):
        super().__init__(daemon=True)
        self.progress_bar: widgets.IntProgress = progress_bar
        self.run_ps_status_out: widgets.Output = run_ps_status_out
        self.msa_view = msa_view
        self.msa_view.clear_output(wait=True)
        self.run_proteinsolver_button: widgets.Button = run_proteinsolver_button

        self.num_designs = None
        self._start_new_design = False
        self._run_condition = threading.Condition()
        self._cancel_event = threading.Event()

    def start_new_design(self, data, num_designs) -> None:
        with self._run_condition:
            self.num_designs = num_designs
            self._start_new_design = True
            self._run_condition.notify()

    def run(self):
        with self._run_condition:
            while True:
                while not self._start_new_design:
                    self._run_condition.wait()

                self._start_new_design = False
                self._cancel_event.clear()

                update_run_ps_button_state(self.run_proteinsolver_button, State.DISABLED)
                self.progress_bar.value = 0
                self.progress_bar.bar_style = ""
                self.progress_bar.max = self.num_designs
                self.msa_view.clear_output(wait=True)

                global_state.generated_sequences = [
                    MSASeq(0, "Reference", "".join(global_state.reference_sequence), True),
                    MSASeq(1, "Target", "".join(global_state.target_sequence), True),
                ]
                for seq in global_state.generated_sequences:
                    self.msa_view.append_stdout(f"{seq.name:10s}: {seq.seq}\n")

                data = global_state.data
                data.x = torch.tensor(
                    [AMINO_ACID_TO_IDX[aa] for aa in global_state.target_sequence],
                    dtype=torch.long,
                )
                proc = ProteinSolverProcess(
                    net_class=global_state.net_class,
                    state_file=global_state.state_file,
                    data=data,
                    num_designs=self.num_designs,
                    net_kwargs=global_state.net_kwargs,
                )
                proc.start()

                success = True
                while len(global_state.generated_sequences) < (self.num_designs + 2):
                    if self.cancelled():
                        success = False
                        proc.cancel()
                        break

                    try:
                        design = proc.output_queue.get(timeout=1.0)
                    except queue.Empty:
                        continue

                    if isinstance(design, Exception):
                        logger.error(f"Encountered an exception: ({type(design)} - {design}).")
                        with self.run_ps_status_out:
                            display(f"Encountered an exception: ({type(design)} - {design}).")
                        success = False
                        proc.cancel()
                        break
                    else:
                        design.id += 2
                        global_state.generated_sequences.append(design)
                        self.progress_bar.value += 1
                        self.msa_view.append_stdout(f"{design.name:10s}: {design.seq}\n")

                if success:
                    self.progress_bar.bar_style = "success"
                else:
                    self.progress_bar.bar_style = "danger"

                proc.join()
                update_run_ps_button_state(self.run_proteinsolver_button, State.ENABLED)

    def cancel(self):
        self._cancel_event.set()

    def cancelled(self):
        return self._cancel_event.is_set()


def update_run_ps_button_state(run_ps_button: widgets.Button, state: State):
    run_ps_button.description = button_states[state]["description"]
    run_ps_button.icon = button_states[state]["icon"]
    run_ps_button.button_style = button_states[state]["button_style"]
    run_ps_button.tooltip = button_states[state]["tooltip"]


def on_run_ps_button_clicked(run_ps_button, num_designs_field):
    if run_ps_button.description == button_states[State.ENABLED]["description"]:
        update_run_ps_button_state(run_ps_button, State.DISABLED)
        global_state.proteinsolver_thread.cancel()
        global_state.proteinsolver_thread.start_new_design(
            global_state.data, num_designs_field.value
        )
    else:
        assert run_ps_button.description == button_states[State.DISABLED]["description"]
        global_state.proteinsolver_thread.cancel()
        update_run_ps_button_state(run_ps_button, State.ENABLED)


def update_sequence_generation(sequence_generation_out):
    sequence_generation_out.clear_output(wait=True)
    html_string = (
        '<p class="myheading" style="margin-top: 3rem">'
        "3. Run ProteinSolver to generate new designs"
        "</p>"
    )
    sequence_generation_widget = get_or_create_sequence_generation_widget()
    with sequence_generation_out:
        display(HTML(html_string))
        display(sequence_generation_widget)


# @lru_cache()
def get_or_create_sequence_generation_widget():
    num_designs_field = widgets.BoundedIntText(
        value=100,
        min=1,
        max=20_000,
        step=1,
        description="Number of sequences:",
        disabled=False,
        style={"description_width": "initial"},
        layout=widgets.Layout(width="auto"),
    )

    run_ps_button = widgets.Button(layout=widgets.Layout(width="auto"))
    update_run_ps_button_state(run_ps_button, State.ENABLED)
    run_ps_button.on_click(partial(on_run_ps_button_clicked, num_designs_field=num_designs_field))

    run_ps_status_out = widgets.Output(layout=widgets.Layout(height="50px"))

    progress_bar = widgets.IntProgress(
        value=0,
        min=0,
        max=100,
        step=1,
        bar_style="",  # 'success', 'info', 'warning', 'danger' or ''
        orientation="horizontal",
        layout=widgets.Layout(width="auto", height="15px"),
    )

    msa_view = widgets.Output(layout=widgets.Layout(width="auto"))

    # if global_state.proteinsolver_thread is None:
    global_state.proteinsolver_thread = ProteinSolverThread(
        progress_bar, run_ps_status_out, msa_view, run_ps_button
    )
    global_state.proteinsolver_thread.start()

    # The remaining widgets are stateless with respect to sequence generation

    gpu_utilization_widget, gpu_error_message = create_gpu_status_widget()
    gpu_status_out = widgets.Output(layout=widgets.Layout(height="50px"))
    if gpu_error_message:
        gpu_status_out.append_stdout(f"<p>GPU monitoring not available ({gpu_error_message}).</p>")

    download_button = create_download_button(global_state.output_folder)

    # Put everything together

    left_panel = widgets.VBox(
        [
            widgets.VBox([num_designs_field, run_ps_button]),
            run_ps_status_out,
            gpu_utilization_widget,
            gpu_status_out,
            download_button,
        ],
        layout=widgets.Layout(
            flex_flow="column nowrap",
            justify_content="flex-start",
            width="240px",
            margin="0px 20px 0px 0px",
        ),
    )
    right_panel = widgets.VBox(
        [progress_bar, msa_view], layout=widgets.Layout(width="auto", flex="1 1 auto"),
    )

    return widgets.HBox([left_panel, right_panel], layout=widgets.Layout(flex_flow="row nowrap"))
