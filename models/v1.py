import logging

import pytorch_lightning as pl
import torch
import torch.nn as nn
import wandb
from torch import optim
from transformers import ElectraModel, BatchEncoding

import utils

logger = logging.getLogger(__name__)


class PositionalEncoding(nn.Module):
    def __init__(
        self,
        d_model: int = 512,
        max_len: int = 5000,
        scale: int = 10000,
        dropout: float = 0.1,
    ):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(dropout)

        # Compute the positional encodings in log space
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2) * -(torch.log(torch.Tensor([scale])) / d_model)
        )
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)
        self.register_buffer("pe", pe)

    def forward(self, x):
        x = x + self.pe[:, : x.size(1)]
        return self.dropout(x)


class PositionalMultiheadAttention(nn.Module):
    def __init__(
        self,
        d_model: int = 512,
        num_heads: int = 8,
        max_len: int = 5000,
        scale: int = 10000,
        dropout: float = 0.1,
        *args,
        **kwargs,
    ):
        super(PositionalMultiheadAttention, self).__init__(*args, **kwargs)

        self.positional_encoding = PositionalEncoding(
            d_model, max_len=max_len, scale=scale, dropout=dropout
        )
        self.multi_head_attention = nn.MultiheadAttention(d_model, num_heads, dropout=dropout)

    def forward(
        self,
        x: torch.Tensor,
        key_padding_mask: torch.Tensor = None,
        use_positional_encoding: bool = True,
    ):
        if use_positional_encoding:
            x = self.positional_encoding(x)

        context, context_weights = self.multi_head_attention(
            x, x, x, key_padding_mask=key_padding_mask
        )
        return context, context_weights


class DocEncoder(nn.Module):
    def __init__(
        self,
        max_length: int = 20,
        model_name: str = "monologg/koelectra-base-v3-discriminator",
    ):
        super(DocEncoder, self).__init__()
        self.model = ElectraModel.from_pretrained(model_name)
        self.max_length = max_length

    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor):
        outputs = self.model(input_ids=input_ids, attention_mask=attention_mask)
        embeddings = outputs.last_hidden_state
        return embeddings


class DeprecatedAdditiveAttention(nn.Module):
    """Additive Attention learns the importance of each word in the sequence."""

    def __init__(self, embed_dim: int = 768, output_dim: int = 128):
        super(DeprecatedAdditiveAttention, self).__init__()
        self.linear = nn.Linear(embed_dim, output_dim, bias=True)
        self.query = nn.Parameter(torch.randn(output_dim))
        self.tanh = nn.Tanh()
        self.softmax = nn.Softmax(dim=1)

        nn.init.xavier_normal_(self.linear.weight)

    def forward(self, x: torch.Tensor):
        x_proj = self.linear(x)
        x_proj = self.tanh(x_proj)
        attn_scores = torch.matmul(x_proj, self.query)
        attn_probs = self.softmax(attn_scores)
        return attn_probs


class AdditiveAttention(nn.Module):
    """Additive Attention learns the importance of each word in the sequence."""

    def __init__(self, embed_dim: int = 768, output_dim: int = 128, dropout: float = 0.2):
        super(AdditiveAttention, self).__init__()

        self.proj = nn.Linear(embed_dim, output_dim, bias=True)
        self.tanh = nn.Tanh()
        self.proj_v = nn.Linear(output_dim, 1, bias=False)  # No bias for this layer, similar to A.
        self.dropout = nn.Dropout(dropout)
        self.softmax = nn.Softmax(dim=1)

        nn.init.xavier_normal_(self.proj.weight)

    def forward(self, x: torch.Tensor):
        x_proj = self.proj(x)
        x_proj = self.tanh(x_proj)
        # x_proj = self.dropout(x_proj)
        attn_scores = self.proj_v(x_proj).squeeze(-1)
        attn_probs = self.softmax(attn_scores)
        return attn_probs


class NewsEncoder(nn.Module):
    def __init__(
        self,
        embed_dim: int = 768,
        output_dim: int = 128,
        num_heads: int = 8,
        dropout: float = 0.2,
    ):
        super(NewsEncoder, self).__init__()
        self.multi_head_attention = nn.MultiheadAttention(
            embed_dim, num_heads, batch_first=True, dropout=dropout
        )
        self.additive_attention = AdditiveAttention(
            embed_dim=embed_dim, output_dim=output_dim, dropout=dropout
        )
        self.linear = nn.Linear(embed_dim, output_dim)
        self.tanh = nn.Tanh()

        nn.init.xavier_normal_(self.linear.weight)

    def forward(
        self,
        x: torch.Tensor,
        key_padding_mask: torch.Tensor = None,
        softmax_padding_mask: torch.Tensor = None,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        logger.debug(f"x shape: {x.shape}")

        assert key_padding_mask.shape == (x.shape[0], x.shape[1])

        # Multi-head attention
        context, context_weights = self.multi_head_attention(
            x, x, x, key_padding_mask=key_padding_mask
        )
        logger.debug(f"context shape: {context.shape}")

        # Fully connected layer
        transformed_context = self.linear(context)
        transformed_context = self.tanh(transformed_context)
        logger.debug(f"transformed context shape: {transformed_context.shape}")

        # Additive attention
        additive_weights = self.additive_attention(context)
        if softmax_padding_mask is not None:
            additive_weights = additive_weights * softmax_padding_mask

        # Weighted context by the attention weights
        out = torch.sum(additive_weights.unsqueeze(-1) * transformed_context, dim=1)
        logger.debug(f"news encoder out shape: {out.shape}")
        return out, context_weights, additive_weights


class UserEncoder(nn.Module):
    def __init__(
        self,
        embed_dim: int = 128,
        num_heads: int = 8,
        dropout: float = 0.2,
    ):
        super(UserEncoder, self).__init__()
        self.multi_head_attention = nn.MultiheadAttention(
            embed_dim, num_heads, batch_first=True, dropout=dropout
        )
        self.additive_attention = AdditiveAttention(
            embed_dim=embed_dim, output_dim=embed_dim, dropout=dropout
        )
        self.linear = nn.Linear(embed_dim, embed_dim)
        self.tanh = nn.Tanh()

        nn.init.xavier_normal_(self.linear.weight)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        logger.debug(f"x shape: {x.shape}")

        # Multi-head attention
        context, _ = self.multi_head_attention(x, x, x)
        logger.debug(f"context shape: {context.shape}")

        # Fully connected layer
        transformed_context = self.linear(context)
        transformed_context = self.tanh(transformed_context)
        logger.debug(f"transformed context shape: {transformed_context.shape}")

        # Additive attention
        additive_weights = self.additive_attention(context)

        # Weighted context by the attention weights
        out = torch.sum(additive_weights.unsqueeze(-1) * transformed_context, dim=1)
        logger.debug(f"user encoder out shape: {out.shape}")

        return out


class NRMS(pl.LightningModule):
    def __init__(
        self,
        embed_dim: int = 768,
        encoder_dim: int = 128,
        num_heads_news_encoder: int = 8,
        num_heads_user_encoder: int = 8,
        lr: float = 2e-4,
        weight_decay: float = 1e-5,
        dropout: float = 0.2,
    ):
        super(NRMS, self).__init__()

        self.embed_dim = embed_dim
        self.encoder_dim = encoder_dim  # news & user encoder output dimension
        self.num_heads_news_encoder = num_heads_news_encoder
        self.num_heads_user_encoder = num_heads_user_encoder
        self.lr = lr
        self.weight_decay = weight_decay
        self.dropout = dropout

        self.doc_encoder = DocEncoder()

        for param in self.doc_encoder.model.parameters():
            param.requires_grad = False

        for param in self.doc_encoder.model.encoder.layer[-1].parameters():
            param.requires_grad = True

        self.news_encoder = NewsEncoder(
            embed_dim=embed_dim,
            output_dim=encoder_dim,
            num_heads=num_heads_news_encoder,
            dropout=dropout,
        )
        self.user_encoder = UserEncoder(embed_dim=encoder_dim, dropout=dropout)
        self.criterion = nn.CrossEntropyLoss()

        self.training_step_outputs = []
        self.validating_step_outputs = []
        self.testing_step_outputs = []

        self.save_hyperparameters()

    def forward(
        self,
        clicked_ids: torch.Tensor = None,
        clicked_attention_mask: torch.Tensor = None,
        clicked_key_padding_mask: torch.Tensor = None,
        clicked_softmax_padding_mask: torch.Tensor = None,
        labeled_ids: torch.Tensor = None,
        labeled_attention_mask: torch.Tensor = None,
        labeled_key_padding_mask: torch.Tensor = None,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Forward pass for the NRMS model.

        Args:
            clicked_ids (torch.Tensor): Input token ids for the clicked news. (users, titles, seq_length)
            clicked_attention_mask (torch.Tensor): Attention mask for the clicked news. (users, titles, seq_length)
            clicked_key_padding_mask (torch.Tensor): Attention mask for the clicked news. (users, titles, seq_length)
            clicked_softmax_padding_mask (torch.Tensor): Softmax padding mask for the clicked news. (users, titles, seq_length)
            labeled_ids (torch.Tensor): Input token ids for the labeled news. (users, titles, seq_length)
            labeled_attention_mask (torch.Tensor): Attention mask for the labeled news. (users, titles, seq_length)
            labeled_key_padding_mask (torch.Tensor): Attention mask for the labeled news. (users, titles, seq_length)
        Returns:
            torch.Tensor: Scores for each candidate news. (users, K)
            torch.Tensor: Candidate Context Weights.
            torch.Tensor: Candidate Additive Weights.
        """

        if clicked_ids is None:
            raise ValueError("clicked_ids must be provided.")

        if clicked_attention_mask is None:
            raise ValueError("clicked_attention_mask must be provided.")

        if clicked_key_padding_mask is None:
            raise ValueError("clicked_key_padding_mask must be provided.")

        if labeled_ids is None:
            raise ValueError("labeled_ids must be provided.")

        if labeled_attention_mask is None:
            raise ValueError("labeled_attention_mask must be provided.")

        if labeled_key_padding_mask is None:
            raise ValueError("labeled_key_padding_mask must be provided.")

        # labeled
        news_vectors, c_weights, a_weights = self.forward_news_encoder(
            input_ids=labeled_ids,
            attention_mask=labeled_attention_mask,
            key_padding_mask=labeled_key_padding_mask,
        )
        # shape: (users, K + 1, encoder_dim)

        # clicked
        user_vectors = self.forward_user_encoder(
            input_ids=clicked_ids,
            attention_mask=clicked_attention_mask,
            key_padding_mask=clicked_key_padding_mask,
            softmax_padding_mask=clicked_softmax_padding_mask,
        )
        # shape: (users, embed_size)

        # 각 뉴스와 사용자 벡터 간의 내적을 계산하여 scores 계산
        scores = torch.bmm(news_vectors, user_vectors.unsqueeze(2)).squeeze(2)
        # shape: (users, K + 1)

        return scores, c_weights, a_weights

    def forward_doc_encoder(self, input_ids: torch.Tensor, attention_mask: torch.Tensor):
        """Forward pass for the document encoder.

        Args:
            input_ids (torch.Tensor): Input token ids. (users, titles, seq_length)
            attention_mask (torch.Tensor): Attention mask. (users, titles, seq_length)

        Returns:
            torch.Tensor: Document embeddings. (users, titles, seq_length, embed_size)
        """
        users, titles, seq_length = input_ids.shape

        # reshape input_ids and attention_mask
        reshaped_input_ids = input_ids.view(users * titles, seq_length)
        reshaped_attention_mask = attention_mask.view(users * titles, seq_length)

        # forward
        embeddings = self.doc_encoder(reshaped_input_ids, reshaped_attention_mask)
        # rollback the shape and order
        embeddings = embeddings.view(users, titles, seq_length, self.embed_dim)
        return embeddings  # shape: (users, titles, seq_length, embed_size)

    def forward_news_encoder(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        key_padding_mask: torch.Tensor,
        softmax_padding_mask: torch.Tensor = None,
    ):
        """Forward pass for the news encoder.

        Args:
            input_ids (torch.Tensor): Input token ids. (users, titles, seq_length)
            attention_mask (torch.Tensor): Attention mask. (users, titles, seq_length)
            key_padding_mask (torch.Tensor): Key Padding mask. (users, titles, seq_length)
            softmax_padding_mask (torch.Tensor): Softmax padding mask. (users, titles, seq_length)

        Returns:
            torch.Tensor: News embeddings. (users, titles, encoder_dim)
        """

        embeddings = self.forward_doc_encoder(input_ids, attention_mask)
        users, titles, seq_length, embed_size = embeddings.shape

        embeddings = embeddings.view(users * titles, seq_length, embed_size)
        key_padding_mask = key_padding_mask.view(users * titles, seq_length)
        if softmax_padding_mask is not None:
            softmax_padding_mask = softmax_padding_mask.view(users * titles, seq_length)

        news_vectors, c_weights, a_weights = self.news_encoder(
            embeddings,
            key_padding_mask=key_padding_mask,
            softmax_padding_mask=softmax_padding_mask,
        )
        news_vectors = news_vectors.view(users, titles, self.encoder_dim)
        return news_vectors, c_weights, a_weights

    def forward_user_encoder(
        self,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        key_padding_mask: torch.Tensor,
        softmax_padding_mask: torch.Tensor = None,
    ):
        """Forward pass for the user encoder.

        Args:
            input_ids (torch.Tensor): Input token ids. (users, titles, seq_length)
            attention_mask (torch.Tensor): Attention mask. (users, titles, seq_length)
            key_padding_mask (torch.Tensor): Key padding mask. (users, titles, seq_length)
            softmax_padding_mask (torch.Tensor): Softmax padding mask. (users, titles, seq_length)

        Returns:
            torch.Tensor: User embeddings. (users, encoder_dim)
        """
        news_vectors, _, __ = self.forward_news_encoder(
            input_ids=input_ids,
            attention_mask=attention_mask,
            key_padding_mask=key_padding_mask,
            softmax_padding_mask=softmax_padding_mask,
        )
        users, titles, encoder_dim = news_vectors.shape

        # FORWARD HERE
        user_vector = self.user_encoder(news_vectors)

        # USER 가 읽은 기사가 모두 합쳐져서 벡터를 생성 하므로.. TITLE 개수 차원이 merge 되어야 함
        assert user_vector.shape == (users, encoder_dim)
        return user_vector

    def forward_and_compute_loss(self, batch: BatchEncoding):
        clicked_tokens, labeled_tokens, labels = batch

        scores, c_weights, a_weights = self.forward(
            clicked_ids=clicked_tokens["input_ids"],
            clicked_attention_mask=clicked_tokens["attention_mask"],
            clicked_key_padding_mask=clicked_tokens["key_padding_mask"],
            clicked_softmax_padding_mask=clicked_tokens["softmax_padding_mask"],
            labeled_ids=labeled_tokens["input_ids"],
            labeled_attention_mask=labeled_tokens["attention_mask"],
            labeled_key_padding_mask=labeled_tokens["key_padding_mask"],
        )

        assert labels.shape == (scores.shape[0], 1)
        labels = labels.squeeze(1).long()

        loss = self.criterion(scores, labels)
        return loss, c_weights, a_weights

    def configure_optimizers(self):
        optimizer = optim.Adam(self.parameters(), lr=self.lr, weight_decay=self.weight_decay)
        # scheduler = ReduceLROnPlateau(optimizer, mode="min", factor=0.1, patience=5)
        return {
            "optimizer": optimizer,
            # "lr_scheduler": {
            #     "scheduler": scheduler,
            #     "monitor": "avg_val_loss",
            # },
        }

    def training_step(self, batch: BatchEncoding, batch_idx: int):
        loss, _, __ = self.forward_and_compute_loss(batch)

        self.log("train_loss", loss, on_step=True, on_epoch=True, logger=True)
        self.training_step_outputs.append(loss)
        return loss

    def validation_step(self, batch: BatchEncoding, batch_idx: int):
        loss, c_weights, a_weights = self.forward_and_compute_loss(batch)

        self.log("val_loss", loss, on_step=True, on_epoch=True, logger=True)
        self.validating_step_outputs.append(loss)

        if batch_idx % 3 == 0:
            # Attention visualization logging
            title = "Attention Weights"
            caption = f"{title} Batch-{batch_idx}"
            fig = utils.plot_2d_weights(weights=c_weights[0], title=title)
            wandb.log({"attention_weights": [wandb.Image(fig, caption=caption)]})

            # Additive softmax_results visualization logging
            title = "Additive Softmax"
            caption = f"{title} Batch-{batch_idx}"
            fig = utils.plot_2d_weights(weights=a_weights[:20], title=title)
            wandb.log({"additive_softmax": [wandb.Image(fig, caption=caption)]})

        return {"val_loss": loss}

    def test_step(self, batch: BatchEncoding, batch_idx: int):
        loss, _, __ = self.forward_and_compute_loss(batch)

        self.log("test_loss", loss, on_step=True, on_epoch=True, logger=True)
        self.testing_step_outputs.append(loss)
        return {"test_loss": loss}

    def on_train_epoch_end(self) -> None:
        avg_loss = torch.stack([x for x in self.training_step_outputs]).mean()
        self.log("avg_train_loss", avg_loss, prog_bar=True)
        self.training_step_outputs.clear()

    def on_validation_epoch_end(self) -> None:
        avg_loss = torch.stack([x for x in self.validating_step_outputs]).mean()  # 수정된 부분
        self.log("avg_val_loss", avg_loss, prog_bar=True)
        self.validating_step_outputs.clear()

    def on_test_epoch_end(self) -> None:
        avg_loss = torch.stack([x for x in self.testing_step_outputs]).mean()
        self.log("avg_test_loss", avg_loss, prog_bar=True)
        self.testing_step_outputs.clear()
