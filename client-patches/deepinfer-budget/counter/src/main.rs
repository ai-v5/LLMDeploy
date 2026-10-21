//! Count the fully serialized V4.1 request without inference or output clamping.
use deepseek_recipe::openai::ChatCompletionRequest;
use deepseek_recipe::request::{ConversionOptions, ProtocolRequest};
use deepseek_recipe_core::multimodal::ImageTokenSpec;
use deepseek_recipe_encoding::v4::dsv41::DeepseekV41Encoding;
use deepseek_recipe_encoding::{PromptEncoding, PromptTokenizer};
use std::io::{self, Read};
use tokenizers::Tokenizer;

fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let tokenizer_path = std::env::args().nth(1).ok_or("tokenizer path required")?;
    let mut body = String::new();
    io::stdin().read_to_string(&mut body)?;
    let mut value: serde_json::Value = serde_json::from_str(&body)?;
    // The deployment's model id is an alias for the V4.1 recipe.
    value["model"] = "DeepSeek-V4.1-Flash".into();
    // Match recipe_api.prepare_request's OpenAI compatibility normalization.
    if let Some(messages) = value["messages"].as_array_mut() {
        for message in messages {
            if message["role"] == "developer" {
                message["role"] = "system".into();
            }
        }
    }
    if let Some(object) = value.as_object_mut() {
        if let Some(completion) = object.remove("max_completion_tokens") {
            if object.get("max_tokens").is_some_and(|tokens| tokens != &completion) {
                return Err("conflicting output budget fields".into());
            }
            object.insert("max_tokens".into(), completion);
        }
        object.remove("prompt_cache_key");
        object.remove("ignore_eos");
    }
    let request: ChatCompletionRequest = serde_json::from_value(value)?;
    let converted = request.convert(ConversionOptions::default())?;
    let tokenizer = Tokenizer::from_file(tokenizer_path)?;
    let encoding = DeepseekV41Encoding::new().with_tokenizer(PromptTokenizer::new(&tokenizer));
    let rendered = encoding.render_conversation(&converted.conversation);
    // The server's V4.1 preprocessor fits each image to this recipe-owned cap.
    // Text/templates are exact; images reserve their upper bound without fetching.
    let image_reservation = rendered.image_sources.len()
        * (ImageTokenSpec::v41().max_token_count() - 1);
    let tokens = encoding.encode(&converted.conversation)?.len() + image_reservation;
    println!("{tokens}");
    Ok(())
}
